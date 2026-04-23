{
  description = "pytimefliplib development and Bumble-backed TimeFlip simulator tests";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python313;
          pythonPackages = pkgs.python313Packages;

          bumble = pythonPackages.buildPythonPackage rec {
            pname = "bumble";
            version = "0.0.228";
            pyproject = true;

            src = pythonPackages.fetchPypi {
              inherit pname version;
              hash = "sha256-ePsVZUjdc4oo7LpTnnqqE4EXUWqIEUgumAG801pPu70=";
            };

            build-system = with pythonPackages; [
              setuptools
              setuptools-scm
              wheel
            ];

            dependencies = with pythonPackages; [
              aiohttp
              click
              cryptography
              grpcio
              humanize
              libusb1
              platformdirs
              prettytable
              prompt-toolkit
              protobuf
              pyee
              pyserial
              pyserial-asyncio
              pyusb
              websockets
            ];

            pythonRemoveDeps = [ "libusb-package" ];
            pythonRelaxDeps = [
              "cryptography"
              "websockets"
            ];

            doCheck = false;
          };

          pytimefliplib = pythonPackages.buildPythonPackage {
            pname = "pytimefliplib";
            version = self.shortRev or "dirty";
            pyproject = true;

            src = ./.;

            build-system = with pythonPackages; [
              setuptools
              wheel
            ];

            dependencies = with pythonPackages; [
              bleak
            ];

            doCheck = false;
          };

          testPython = python.withPackages (
            ps: [
              bumble
              pytimefliplib
              ps.bleak
            ]
          );
        in
        {
          inherit bumble pytimefliplib testPython;

          timeflip-bumble-simulator = pkgs.writeShellApplication {
            name = "timeflip-bumble-simulator";
            runtimeInputs = [ testPython ];
            text = ''
              exec ${testPython}/bin/python ${./nix/timeflip_bumble_simulator.py} "$@"
            '';
          };

          timeflip-bumble-client-test = pkgs.writeShellApplication {
            name = "timeflip-bumble-client-test";
            runtimeInputs = [ testPython ];
            text = ''
              exec ${testPython}/bin/python ${./nix/timeflip_bumble_client_test.py} "$@"
            '';
          };

          timeflip-physical-device-details = pkgs.writeShellApplication {
            name = "timeflip-physical-device-details";
            runtimeInputs = [ testPython ];
            text = ''
              exec ${testPython}/bin/python ${./nix/timeflip_physical_device_details.py} "$@"
            '';
          };

          timeflip-raw-history-dump = pkgs.writeShellApplication {
            name = "timeflip-raw-history-dump";
            runtimeInputs = [ testPython ];
            text = ''
              exec ${testPython}/bin/python ${./nix/timeflip_raw_history_dump.py} "$@"
            '';
          };
        }
      );

      checks = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          packages = self.packages.${system};
        in
        {
          timeflip-bumble = pkgs.testers.nixosTest {
            name = "pytimefliplib-timeflip-bumble";

            nodes.machine =
              { pkgs, ... }:
              {
                services.dbus.enable = true;
                hardware.bluetooth.enable = true;
                hardware.bluetooth.powerOnBoot = true;
                hardware.bluetooth.settings.General.Privacy = "off";

                boot.kernelModules = [
                  "bluetooth"
                  "hci_uart"
                ];

                environment.systemPackages = [
                  pkgs.bluez
                  pkgs.coreutils
                  pkgs.iproute2
                  pkgs.procps
                  packages.timeflip-bumble-client-test
                  packages.timeflip-bumble-simulator
                ];
              };

            testScript = ''
              machine.start()
              machine.wait_for_unit("multi-user.target")
              machine.wait_for_unit("dbus.service")
              machine.succeed("timeflip-bumble-simulator --pty /tmp/timeflip-hci --control /tmp/timeflip-control.sock > /tmp/timeflip-simulator.log 2>&1 &")
              machine.wait_until_succeeds("test -e /tmp/timeflip-hci")
              machine.wait_until_succeeds("grep -q 'timeflip simulator ready' /tmp/timeflip-simulator.log")
              machine.succeed("btattach -P h4 -B /tmp/timeflip-hci > /tmp/btattach.log 2>&1 &")
              machine.wait_until_succeeds("hciconfig hci0 >/dev/null 2>&1")
              machine.succeed("hciconfig hci0 up")
              machine.succeed("systemctl start bluetooth.service")
              machine.wait_for_unit("bluetooth.service")
              machine.succeed("bluetoothctl power on")
              machine.wait_until_succeeds("bluetoothctl list | grep -q Controller")
              machine.succeed("cat /tmp/timeflip-simulator.log")
              machine.succeed("timeflip-bumble-client-test --control /tmp/timeflip-control.sock || (cat /tmp/timeflip-simulator.log; bluetoothctl devices; exit 1)")
              machine.succeed("cat /tmp/timeflip-simulator.log")
            '';
          };
        }
      );

      apps = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = self.apps.${system}.timeflip-bumble-test;

          timeflip-bumble-test = {
            type = "app";
            program = "${pkgs.writeShellScript "run-timeflip-bumble-test" ''
              set -euo pipefail
              exec ${self.checks.${system}.timeflip-bumble.driver}/bin/nixos-test-driver
            ''}";
          };

          timeflip-physical-device-details = {
            type = "app";
            program = "${self.packages.${system}.timeflip-physical-device-details}/bin/timeflip-physical-device-details";
          };

          timeflip-raw-history-dump = {
            type = "app";
            program = "${self.packages.${system}.timeflip-raw-history-dump}/bin/timeflip-raw-history-dump";
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          packages = self.packages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              packages.testPython
              pkgs.bluez
            ];
          };
        }
      );
    };
}
