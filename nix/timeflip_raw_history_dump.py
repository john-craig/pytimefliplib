import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from bleak import BleakClient

from pytimefliplib.async_client import (
    CHARACTERISTICS,
    DEFAULT_PASSWORD,
    TIMEFLIP_ENDIANNESS,
)


def parse_env_line(line: str, line_number: int) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        raise ValueError(f"line {line_number}: expected KEY=VALUE")

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = parse_env_line(line, line_number)
        if parsed is not None:
            key, value = parsed
            values[key] = value
    return values


def hex_bytes(data: bytes | bytearray) -> str:
    return bytes(data).hex(" ")


def decode_candidate_blocks(frame: bytes) -> list[dict[str, Any]]:
    blocks = []
    for index in range(0, len(frame) - 2, 3):
        raw = frame[index : index + 3]
        if len(raw) < 3:
            break
        duration_big = int.from_bytes(bytes([raw[0], raw[1], raw[2] & 0x03]), TIMEFLIP_ENDIANNESS)
        duration_little = raw[0] | (raw[1] << 8) | ((raw[2] & 0x03) << 16)
        blocks.append(
            {
                "index": index // 3,
                "raw": hex_bytes(raw),
                "facet_high6": raw[2] >> 2,
                "facet_low6": raw[2] & 0x3F,
                "duration_big_18bit": duration_big,
                "duration_little_18bit": duration_little,
                "duration_24bit_big": int.from_bytes(raw, "big"),
                "duration_24bit_little": int.from_bytes(raw, "little"),
            }
        )
    return blocks


async def read_repeated(client: BleakClient, characteristic: str, reads: int, delay: float) -> list[dict[str, Any]]:
    rows = []
    previous = None
    repeated = 0
    for read_index in range(reads):
        data = bytes(await client.read_gatt_char(CHARACTERISTICS[characteristic]))
        rows.append(
            {
                "read_index": read_index,
                "length": len(data),
                "hex": hex_bytes(data),
                "all_zero": all(value == 0 for value in data),
                "same_as_previous": data == previous,
                "candidate_blocks": decode_candidate_blocks(data),
            }
        )
        if data == previous:
            repeated += 1
        else:
            repeated = 0
        previous = data
        if all(value == 0 for value in data) or repeated >= 2:
            break
        await asyncio.sleep(delay)
    return rows


async def try_v3_history_command(
    client: BleakClient, *, padded: bool, reads: int, delay: float
) -> dict[str, Any]:
    command = bytes([0x01]).ljust(20, b"\x00") if padded else bytes([0x01])
    await client.write_gatt_char(CHARACTERISTICS["command_input"], command, response=True)
    command_echo = await client.read_gatt_char(CHARACTERISTICS["command_input"])
    return {
        "command": hex_bytes(command),
        "command_echo": hex_bytes(command_echo),
        "reads": await read_repeated(client, "command_result", reads, delay),
    }


async def try_v4_history_command(
    client: BleakClient, *, command_id: int, event_number: int, reads: int, delay: float
) -> dict[str, Any]:
    command = bytearray(5)
    command[0] = command_id
    command[1:5] = event_number.to_bytes(4, "big")
    await client.write_gatt_char(CHARACTERISTICS["history_data"], command, response=True)
    return {
        "command": hex_bytes(command),
        "reads": await read_repeated(client, "history_data", reads, delay),
    }


async def try_v4_history_notifications(
    client: BleakClient, *, event_number: int, wait_seconds: float
) -> dict[str, Any]:
    notifications: list[dict[str, Any]] = []

    def callback(_sender, data: bytearray) -> None:
        frame = bytes(data)
        notifications.append(
            {
                "notification_index": len(notifications),
                "length": len(frame),
                "hex": hex_bytes(frame),
                "all_zero_first_17": len(frame) >= 17 and all(value == 0 for value in frame[:17]),
            }
        )

    command = bytearray(5)
    command[0] = 0x02
    command[1:5] = event_number.to_bytes(4, "big")

    await client.start_notify(CHARACTERISTICS["history_data"], callback)
    try:
        await client.write_gatt_char(CHARACTERISTICS["history_data"], command, response=True)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            if notifications and notifications[-1]["all_zero_first_17"]:
                break
            await asyncio.sleep(0.1)
    finally:
        await client.stop_notify(CHARACTERISTICS["history_data"])

    return {
        "command": hex_bytes(command),
        "notifications": notifications,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Dump raw TimeFlip history command frames.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--reads", type=int, default=32)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--notify-wait", type=float, default=8.0)
    args = parser.parse_args()

    env = {**os.environ, **read_env_file(Path(args.env_file))}
    address = env["TIMEFLIP_ADDRESS"]
    adapter = env.get("TIMEFLIP_ADAPTER", "hci0")
    password = env.get("TIMEFLIP_PASSWORD", DEFAULT_PASSWORD)

    rows = []
    async with BleakClient(address, adapter=adapter, timeout=45.0) as client:
        services = []
        for service in client.services:
            services.append(
                {
                    "uuid": service.uuid,
                    "description": service.description,
                    "characteristics": [
                        {
                            "uuid": characteristic.uuid,
                            "description": characteristic.description,
                            "properties": sorted(characteristic.properties),
                            "handle": characteristic.handle,
                        }
                        for characteristic in service.characteristics
                    ],
                }
            )

        firmware = await client.read_gatt_char(CHARACTERISTICS["firmware_revision"])
        name = await client.read_gatt_char(CHARACTERISTICS["device_name"])
        battery = await client.read_gatt_char(CHARACTERISTICS["battery_level"])
        facet = await client.read_gatt_char(CHARACTERISTICS["facet"])

        await client.write_gatt_char(CHARACTERISTICS["password_input"], password.encode("ascii"), response=True)

        probes = {
            "v3_unpadded_command_input": await try_v3_history_command(
                client, padded=False, reads=args.reads, delay=args.delay
            ),
            "v3_padded_command_input": await try_v3_history_command(
                client, padded=True, reads=args.reads, delay=args.delay
            ),
            "v4_history_data_read_event_0": await try_v4_history_command(
                client, command_id=0x01, event_number=0, reads=args.reads, delay=args.delay
            ),
            "v4_history_data_read_event_last": await try_v4_history_command(
                client, command_id=0x01, event_number=0xFFFFFFFF, reads=args.reads, delay=args.delay
            ),
            "v4_history_data_dump_from_0": await try_v4_history_command(
                client, command_id=0x02, event_number=0, reads=args.reads, delay=args.delay
            ),
            "v4_history_data_dump_from_0_notifications": await try_v4_history_notifications(
                client, event_number=0, wait_seconds=args.notify_wait
            ),
        }

    print(
        json.dumps(
            {
                "address": address,
                "adapter": adapter,
                "device_name": name.decode("ascii", errors="replace").rstrip("\x00"),
                "firmware_revision": firmware.decode("ascii", errors="replace").rstrip("\x00"),
                "battery_level": battery[0] if battery else None,
                "current_facet": facet[0] if facet else None,
                "services": services,
                "history_probes": probes,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
