# `pytimefliplib`

A Python library TimeFlip devices v3 and v4.

The communication protocol (empirically corrected) for the V3 version is described [here](./protocol_v3_corrected.md) (originally described [here](https://github.com/DI-GROUP/TimeFlip.Docs/blob/master/Hardware/BLE_device_commutication_protocol_v3.0_en.md)).

The one concerning V4 is given [here](https://github.com/DI-GROUP/TimeFlip.Docs/blob/master/Hardware/TimeFlip%20BLE%20protocol%20ver4_02.06.2020.md).

## Install and use

```bash
pip install git+https://github.com/pierre-24/pytimefliplib.git
```

Provides, with [a simple Python API](./pytimefliplib/async_client.py), 
convenient scripts to interact with the device:

- Discover TimeFlip devices:
  ```
  $ timeflip-discover 
  Looking around (this can take up to 1 minute) ... Done!
  Results::
  - TimeFlip devices: 0C:61:CF:C7:77:71 (TimeFlip)
  - Other BLE devices: (redacted)
  - Other devices: (redacted)
  ```
  The MAC address of my device is thus `0C:61:CF:C7:77:71`.

- Change its password:
  ```
  $ timeflip-set-passwd -a 98:07:2D:EE:21:0E 123456
  ! Connected to 98:07:2D:EE:21:0E
  ! Password communicated
  ! Changed password to "123456"
  ```
  Don't forget to use `-p` for further interactions!

- Change its name:
  ```
  $ timeflip-set-name -a 98:07:2D:EE:21:0E -p 123456 MyFlip
  ! Connected to 98:07:2D:EE:21:0E
  ! Password communicated
  ! Changed device name from "TimeFlip" to "MyFlip"
  ```

- Get its status:
  ```
  $ timeflip-check -a 98:07:2D:EE:21:0E -p 123456
  ! Connected to 98:07:2D:EE:21:0E
  ! Password communicated
  TimeFlip characteristics::
  - Name: MyFlip
  - Firmware: TFv3.1
  - Battery: 83
  - Calibration: 0
  - Current facet: 9
  - Accelerometer vector: 0.832, -0.438, 0.262
  - Status: {'locked': False, 'paused': False, 'auto_pause_time': 0}
  History::
  - Facet=0, during 2 seconds
  - Facet=1, during 712 seconds
  (...)
  ```

  + Clear its history
  ```
  $ timeflip-clear-history -a 98:07:2D:EE:21:0E -p 123456
  ! Connected to 98:07:2D:EE:21:0E
  ! Password communicated
  ! Cleared history
  ```

As you can see, the options you have to give to every script (except `timeflip-discover`, of course) are:
+ `-a`, the MAC address of the device and, eventually,
+ `-p`, the password (if it differs from the default password, `000000`).

## Nix test apps

This repository includes Nix apps for integration testing:

```bash
nix run .#timeflip-bumble-test
```

The Bumble test starts a NixOS VM, attaches a virtual HCI Bluetooth controller, runs a Bumble-backed TimeFlip simulator, and exercises the library against that simulated device.

To collect details from a real physical TimeFlip, copy `.env.example` to `.env`, fill in the device values, and run:

```bash
nix run .#timeflip-physical-device-details
```

The `.env` file is ignored by git. The physical-device app reads `TIMEFLIP_ADDRESS`, `TIMEFLIP_ADAPTER`, and `TIMEFLIP_PASSWORD` from that file, then prints the collected device details as JSON. If `TIMEFLIP_ADDRESS` is omitted, set `TIMEFLIP_NAME` to discover by advertised name.

Set `TIMEFLIP_READ_HISTORY=true` in `.env` to include history collection in the physical-device output.

For protocol debugging, the raw history dumper probes the v3 command characteristic and the v4 history characteristic, including notification-based history dumps:

```bash
nix run .#timeflip-raw-history-dump
```
