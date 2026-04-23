import argparse
import asyncio
import math

from bleak import BleakScanner

from pytimefliplib.async_client import AsyncClient


async def rotate(control_path: str, facet: int) -> None:
    reader, writer = await asyncio.open_unix_connection(control_path)
    writer.write(f"facet {facet}\n".encode("ascii"))
    await writer.drain()
    response = await reader.readline()
    writer.close()
    await writer.wait_closed()
    if response.strip() != b"ok":
        raise AssertionError(f"simulator rejected facet change: {response!r}")


async def find_timeflip(timeout: float = 20.0) -> str:
    device = await BleakScanner.find_device_by_filter(
        lambda device, advertisement: device.name == "TimeFlip"
        or advertisement.local_name == "TimeFlip",
        timeout=timeout,
    )
    if device is not None:
        return device.address

    devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
    for address, (device, advertisement) in devices.items():
        print(
            "discovered:",
            address,
            device.name,
            advertisement.local_name,
            sorted(str(uuid) for uuid in advertisement.service_uuids),
        )

    for address, (device, advertisement) in devices.items():
        if address.upper() == "F0:F1:F2:F3:F4:F5":
            return device.address

    if device is None:
        raise AssertionError("TimeFlip simulator was not discoverable")


async def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting for expected notification")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", default="/tmp/timeflip-control.sock")
    args = parser.parse_args()

    address = await find_timeflip()
    observed_facets = []

    def on_facet(value: int) -> None:
        observed_facets.append(value)

    async with AsyncClient(address, adapter="hci0") as client:
        await client.setup(facet_callback=on_facet)

        assert await client.device_name() == "TimeFlip"
        assert await client.firmware_revision() == "FW_v3.46"
        assert await client.battery_level() == 83
        assert await client.current_facet(force=True) == 1
        assert await client.current_facet() == 1

        ax, ay, az = await client.get_accelerometer_value_v3()
        assert math.isclose(ax, 0.0, abs_tol=0.001)
        assert math.isclose(ay, 0.0, abs_tol=0.001)
        assert math.isclose(az, 1.0, abs_tol=0.001)

        assert await client.get_calibration_version() == 7
        assert await client.get_status() == {
            "locked": False,
            "paused": False,
            "auto_pause_time": 0,
        }

        assert await client.set_paused(True, force=True) is True
        assert (await client.get_status())["paused"] is True
        assert await client.set_lock(True, force=True) is True
        assert (await client.get_status())["locked"] is True
        assert await client.set_lock(False, force=True) is False
        assert await client.set_auto_pause(12) is None
        assert (await client.get_status())["auto_pause_time"] == 12

        assert await client.get_history_v4(0) == (0, 3, 1700000000, 12)
        second_history_frame = bytearray(17)
        second_history_frame[0:4] = (1).to_bytes(4, "big")
        second_history_frame[4] = 9
        second_history_frame[5:13] = (1700000012).to_bytes(8, "big")
        second_history_frame[13:17] = (34).to_bytes(4, "little")
        assert client.decode_history_v4(second_history_frame) == (1, 9, 1700000012, 34)
        assert client.is_history_v4_terminator(bytearray(20)) is True

        assert await client.set_name("SimFlip") is True
        assert await client.device_name() == "SimFlip"
        assert await client.set_password("123456") is True

        await rotate(args.control, 9)
        await wait_for(lambda: 9 in observed_facets)
        assert await client.current_facet() == 9
        assert await client.current_facet(force=True) == 9

    print("pytimefliplib Bumble simulator integration test passed")


if __name__ == "__main__":
    asyncio.run(main())
