import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from bumble import hci
from bumble.controller import Controller
from bumble.core import AdvertisingData, PhysicalTransport, UUID
from bumble.device import Connection, Device
from bumble.gatt import Characteristic, CharacteristicValue, Service
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport import open_transport


TIMEFLIP_SERVICE_UUID = "f1196f50-71a4-11e6-bdf4-0800200c9a66"
UUID_GENERIC = "0000{:04x}-0000-1000-8000-00805f9b34fb"
UUID_TIMEFLIP = "f119{:04x}-71a4-11e6-bdf4-0800200c9a66"


class SinglePeerController(Controller):
    def on_link_acl_data(self, sender_address, transport, data) -> None:
        if (
            transport == PhysicalTransport.LE
            and sender_address not in self.le_connections
            and len(self.le_connections) == 1
        ):
            self.le_connections[sender_address] = next(iter(self.le_connections.values()))
        super().on_link_acl_data(sender_address, transport, data)


class TimeFlipState:
    def __init__(self) -> None:
        self.name = "TimeFlip"
        self.password = b"000000"
        self.logged_in = False
        self.firmware = b"FW_v3.46"
        self.battery = 83
        self.facet = 1
        self.locked = False
        self.paused = False
        self.auto_pause_time = 0
        self.last_command = bytearray([0x00, 0x02])
        self.command_result_reads = 0
        self.command_result = bytearray(20)
        self.accelerometer = (0, 0, 16384)
        self.facet_characteristic = None
        self.history_characteristic = None
        self.history_value = bytes(20)
        self.device = None
        self.history_events = [
            (0, 3, 1700000000, 12),
            (1, 9, 1700000012, 34),
        ]

    def fixed_ascii(self, value: str | bytes, length: int = 20) -> bytes:
        data = value if isinstance(value, bytes) else value.encode("ascii")
        return data[:length].ljust(length, b"\x00")

    def read_device_name(self, _connection: Connection) -> bytes:
        return self.name.encode("ascii")

    def read_firmware(self, _connection: Connection) -> bytes:
        return self.firmware

    def read_battery(self, _connection: Connection) -> bytes:
        return bytes([self.battery])

    def read_facet(self, _connection: Connection) -> bytes:
        return bytes([self.facet])

    def read_accelerometer(self, _connection: Connection) -> bytes:
        return b"".join(value.to_bytes(2, "little", signed=True) for value in self.accelerometer)

    def read_command_input(self, _connection: Connection) -> bytes:
        return bytes(self.last_command).ljust(20, b"\x00")

    def read_command_result(self, _connection: Connection) -> bytes:
        return bytes(self.command_result)

    def read_calibration_version(self, _connection: Connection) -> bytes:
        return (7).to_bytes(4, "big")

    def read_history(self, _connection: Connection) -> bytes:
        return self.history_value

    def encode_history_event(self, event: tuple[int, int, int, int]) -> bytes:
        event_number, facet, timestamp, duration = event
        data = bytearray(17)
        data[0:4] = event_number.to_bytes(4, "big")
        data[4] = facet
        data[5:13] = timestamp.to_bytes(8, "big")
        data[13:17] = duration.to_bytes(4, "little")
        return bytes(data)

    def write_history(self, _connection: Connection, value: bytes) -> None:
        if not value:
            return

        command_id = value[0]
        event_number = int.from_bytes(value[1:5], "big") if len(value) >= 5 else 0

        if command_id == 0x01:
            if event_number == 0xFFFFFFFF and self.history_events:
                event_number = self.history_events[-1][0]
            event = next((event for event in self.history_events if event[0] == event_number), None)
            if event is None:
                self.history_value = bytes(17)
            else:
                self.history_value = self.encode_history_event(event)
            self.history_characteristic.value = self.history_value
        elif command_id == 0x02:
            asyncio.create_task(self.notify_history_from(event_number))

    async def notify_history_from(self, event_number: int) -> None:
        if self.device is None or self.history_characteristic is None:
            return

        for event in self.history_events:
            if event[0] < event_number:
                continue
            self.history_characteristic.value = self.encode_history_event(event)
            await self.device.notify_subscribers(self.history_characteristic)
            await asyncio.sleep(0.05)

        self.history_characteristic.value = bytes(20)
        await self.device.notify_subscribers(self.history_characteristic)

    def write_password(self, _connection: Connection, value: bytes) -> None:
        self.logged_in = bytes(value) == self.password

    def write_command_input(self, _connection: Connection, value: bytes) -> None:
        command = bytes(value)
        if not command:
            return

        command_id = command[0]
        self.last_command = bytearray([command_id, 0x02])

        if command_id == 0x04:
            self.locked = len(command) > 1 and command[1] == 0x01
        elif command_id == 0x06:
            self.paused = len(command) > 1 and command[1] == 0x01
        elif command_id == 0x05:
            self.auto_pause_time = int.from_bytes(command[1:3], "big")
        elif command_id == 0x10:
            self.command_result = bytearray(
                [
                    0x01 if self.locked else 0x00,
                    0x01 if self.paused else 0x00,
                    *self.auto_pause_time.to_bytes(2, "big"),
                ]
            ).ljust(20, b"\x00")
        elif command_id == 0x15 and len(command) >= 2:
            name_length = command[1]
            self.name = command[2 : 2 + name_length].decode("ascii")
        elif command_id == 0x30 and len(command) == 7:
            self.password = command[1:7]
        elif command_id in (0x01, 0x02, 0x03):
            self.command_result = bytearray(20)

    async def set_facet(self, value: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError("facet must be between 0 and 255")

        self.facet = value
        if self.facet_characteristic is not None and self.device is not None:
            self.facet_characteristic.value = bytes([self.facet])
            await self.device.notify_subscribers(self.facet_characteristic)


class Listener(Device.Listener, Connection.Listener):
    def on_connection(self, connection: Connection) -> None:
        connection.listener = self
        print(f"connected: {connection}", flush=True)

    def on_disconnection(self, reason: int) -> None:
        print(f"disconnected: reason={reason}", flush=True)


def make_device_config() -> str:
    advertising_data = bytes(
        AdvertisingData(
            [
                (
                    AdvertisingData.FLAGS,
                    bytes(
                        [
                            AdvertisingData.LE_GENERAL_DISCOVERABLE_MODE_FLAG
                            | AdvertisingData.BR_EDR_NOT_SUPPORTED_FLAG
                        ]
                    ),
                ),
                (AdvertisingData.COMPLETE_LOCAL_NAME, b"TimeFlip"),
                (
                    AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
                    bytes(UUID(TIMEFLIP_SERVICE_UUID)),
                ),
            ]
        )
    )
    config = {
        "name": "TimeFlip",
        "address": "F0:F1:F2:F3:F4:F5",
        "advertising_data": advertising_data.hex(),
        "advertising_interval": 100,
        "gap_service_enabled": False,
    }
    handle = tempfile.NamedTemporaryFile("w", delete=False)
    with handle:
        json.dump(config, handle)
    return handle.name


def add_timeflip_services(device: Device, state: TimeFlipState) -> None:
    device_name = Characteristic(
        UUID_GENERIC.format(0x2A00),
        Characteristic.Properties.READ,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_device_name),
    )
    battery_level = Characteristic(
        UUID_GENERIC.format(0x2A19),
        Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_battery),
    )
    firmware_revision = Characteristic(
        UUID_GENERIC.format(0x2A26),
        Characteristic.Properties.READ,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_firmware),
    )
    accelerometer = Characteristic(
        UUID_TIMEFLIP.format(0x6F51),
        Characteristic.Properties.READ,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_accelerometer),
    )
    facet = Characteristic(
        UUID_TIMEFLIP.format(0x6F52),
        Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_facet),
    )
    command_result = Characteristic(
        UUID_TIMEFLIP.format(0x6F53),
        Characteristic.Properties.READ,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_command_result),
    )
    command_input = Characteristic(
        UUID_TIMEFLIP.format(0x6F54),
        Characteristic.Properties.READ | Characteristic.Properties.WRITE,
        Characteristic.READABLE | Characteristic.WRITEABLE,
        CharacteristicValue(read=state.read_command_input, write=state.write_command_input),
    )
    calibration_version = Characteristic(
        UUID_TIMEFLIP.format(0x6F56),
        Characteristic.Properties.READ,
        Characteristic.READABLE,
        CharacteristicValue(read=state.read_calibration_version),
    )
    password_input = Characteristic(
        UUID_TIMEFLIP.format(0x6F57),
        Characteristic.Properties.WRITE,
        Characteristic.WRITEABLE,
        CharacteristicValue(write=state.write_password),
    )
    history_data = Characteristic(
        UUID_TIMEFLIP.format(0x6F58),
        Characteristic.Properties.READ | Characteristic.Properties.WRITE | Characteristic.Properties.NOTIFY,
        Characteristic.READABLE | Characteristic.WRITEABLE,
        CharacteristicValue(read=state.read_history, write=state.write_history),
    )

    state.facet_characteristic = facet
    state.history_characteristic = history_data
    state.device = device

    device.add_services(
        [
            Service("00001800-0000-1000-8000-00805f9b34fb", [device_name]),
            Service("0000180f-0000-1000-8000-00805f9b34fb", [battery_level]),
            Service("0000180a-0000-1000-8000-00805f9b34fb", [firmware_revision]),
            Service(
                TIMEFLIP_SERVICE_UUID,
                [
                    accelerometer,
                    facet,
                    command_result,
                    command_input,
                    calibration_version,
                    password_input,
                    history_data,
                ],
            ),
        ]
    )


async def handle_control_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: TimeFlipState) -> None:
    try:
        while line := await reader.readline():
            parts = line.decode("ascii").strip().split()
            if parts[:1] == ["facet"] and len(parts) == 2:
                await state.set_facet(int(parts[1]))
                writer.write(b"ok\n")
            elif parts[:1] == ["status"]:
                writer.write(f"facet {state.facet}\n".encode("ascii"))
            else:
                writer.write(b"error unknown-command\n")
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pty", default="/tmp/timeflip-hci")
    parser.add_argument("--control", default="/tmp/timeflip-control.sock")
    args = parser.parse_args()

    for path in (args.pty, args.control):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    state = TimeFlipState()
    config_path = make_device_config()

    async with await open_transport(f"pty:{args.pty}") as hci_transport:
        link = LocalLink()
        controller = SinglePeerController(
            "bluez-controller",
            host_source=hci_transport.source,
            host_sink=hci_transport.sink,
            link=link,
            public_address="F6:F5:F4:F3:F2:F1",
        )

        peripheral_controller = SinglePeerController(
            "timeflip-controller",
            link=link,
            public_address="F0:F1:F2:F3:F4:F5",
        )
        host = Host()
        host.controller = peripheral_controller

        device = Device.from_config_file(config_path)
        device.host = host
        device.listener = Listener()
        add_timeflip_services(device, state)

        control_server = await asyncio.start_unix_server(
            lambda reader, writer: handle_control_client(reader, writer, state),
            args.control,
        )
        Path(args.control).chmod(0o666)

        await device.power_on()
        await device.start_advertising(auto_restart=True, own_address_type=hci.OwnAddressType.PUBLIC)
        await device.start_scanning()
        print("timeflip simulator ready", flush=True)

        async with control_server:
            await hci_transport.source.terminated


if __name__ == "__main__":
    asyncio.run(main())
