import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from bleak import BleakScanner

from pytimefliplib.async_client import AsyncClient, DEFAULT_PASSWORD


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_env_line(line: str, line_number: int) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        raise ValueError(f"line {line_number}: expected KEY=VALUE")

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise ValueError(f"line {line_number}: empty key")

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return key, value


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")

    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = parse_env_line(line, line_number)
        if parsed is not None:
            key, value = parsed
            values[key] = value
    return values


def value_without_nul_padding(value: str) -> str:
    return value.rstrip("\x00")


async def find_device_by_name(name: str, timeout: float) -> str:
    device = await BleakScanner.find_device_by_filter(
        lambda device, advertisement: device.name == name or advertisement.local_name == name,
        timeout=timeout,
    )
    if device is None:
        raise AssertionError(f"no Bluetooth LE device named {name!r} was discovered")
    return device.address


async def safe_collect(label: str, collector, details: dict[str, Any]) -> None:
    try:
        details[label] = await collector()
    except Exception as exc:
        details[f"{label}_error"] = f"{type(exc).__name__}: {exc}"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Connect to a physical TimeFlip device and collect pytimefliplib details."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to KEY=VALUE settings. Defaults to .env in the current working directory.",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)
    env_values = {**os.environ, **read_env_file(env_path)}

    address = env_values.get("TIMEFLIP_ADDRESS")
    adapter = env_values.get("TIMEFLIP_ADAPTER", "hci0")
    password = env_values.get("TIMEFLIP_PASSWORD", DEFAULT_PASSWORD)
    scan_timeout = float(env_values.get("TIMEFLIP_SCAN_TIMEOUT", "20"))
    notify_seconds = float(env_values.get("TIMEFLIP_NOTIFY_SECONDS", "0"))
    read_history = parse_bool(env_values.get("TIMEFLIP_READ_HISTORY"))

    if not address:
        name = env_values.get("TIMEFLIP_NAME")
        if not name:
            raise ValueError("set TIMEFLIP_ADDRESS in .env, or set TIMEFLIP_NAME to discover by name")
        address = await find_device_by_name(name, scan_timeout)

    observed_facets: list[int] = []

    def on_facet(value: int) -> None:
        observed_facets.append(value)

    details: dict[str, Any] = {
        "address": address,
        "adapter": adapter,
        "env_file": str(env_path),
    }

    async with AsyncClient(address, adapter=adapter) as client:
        await client.setup(facet_callback=on_facet, password=password)

        await safe_collect(
            "device_name",
            lambda: client.device_name(),
            details,
        )
        await safe_collect(
            "firmware_revision",
            lambda: client.firmware_revision(),
            details,
        )
        details["firmware_version"] = client.firmware_version
        await safe_collect("battery_level", lambda: client.battery_level(), details)
        await safe_collect("current_facet", lambda: client.current_facet(force=True), details)
        await safe_collect("status", lambda: client.get_status(), details)
        await safe_collect(
            "accelerometer",
            lambda: client.get_accelerometer_value_v3(),
            details,
        )
        await safe_collect(
            "calibration_version",
            lambda: client.get_calibration_version(),
            details,
        )

        if read_history:
            if client.firmware_version >= 3.47:
                await safe_collect("history", lambda: client.get_all_history(), details)
            else:
                await safe_collect("history", lambda: client.history_v3(), details)

        if notify_seconds > 0:
            await asyncio.sleep(notify_seconds)

        details["observed_facets"] = observed_facets
        details["cached_current_facet"] = await client.current_facet()

    for key in ("device_name", "firmware_revision"):
        if isinstance(details.get(key), str):
            details[key] = value_without_nul_padding(details[key])

    print(json.dumps(details, indent=2, sort_keys=True, default=list))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"timeflip physical device detail collection failed: {exc}", file=sys.stderr)
        raise
