"""Bounded, no-control Venus publisher for commissioning observation.

Safety properties of this stage-only adapter:

* it only receives from an already-configured SocketCAN interface;
* it never transmits CAN or calls a write-capable D-Bus method;
* it refuses to register unless Venus BMS control is explicitly disabled;
* it exits if that setting changes while it is running;
* it has a mandatory finite runtime.

The adapter is intentionally unsuitable for selecting as the controlling BMS.
It omits all ``/Info``, ``/Io`` and ``/Capabilities`` paths so systemcalc does
not classify it as a second BMS.  It exists only to validate D-Bus identity and
measurements alongside the stock driver.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import select
import socket
import sys
import time
from typing import Any, Callable

from .version import VERSION
from .policy import PolicyConfig
from .venus_discovery import VebusLocator, discover_vebus_service
from .venus_runtime import (
    CAN_FRAME_SIZE,
    RuntimeClock,
    SocketCanFrameError,
    VenusPublisherCore,
)


SERVICE_NAME = "com.victronenergy.battery.deye_se_f12"
DEVICE_INSTANCE = 513
NO_BMS_CONTROL = -255
UPDATE_INTERVAL_MS = 250
MINIMUM_DURATION_SECONDS = 15.0
MAXIMUM_DURATION_SECONDS = 600.0
VELIB_PATH = Path("/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
STAGE_EXCLUDED_PREFIXES = ("/Info/", "/Io/", "/Capabilities/")


def _unwrap(value: Any) -> Any:
    return getattr(value, "real", value) if not isinstance(value, bool) else value


def _get_value(bus: Any, service_name: str, path: str) -> Any:
    item = bus.get_object(service_name, path)
    return _unwrap(item.GetValue(dbus_interface="com.victronenergy.BusItem"))


def _read_bms_instance(bus: Any) -> int | None:
    value = _get_value(
        bus,
        "com.victronenergy.settings",
        "/Settings/SystemSetup/BmsInstance",
    )
    return int(value) if isinstance(value, (int, float)) else None


def _read_vebus_voltage(bus: Any, service_name: str | None) -> float | None:
    if service_name is None:
        return None
    try:
        value = _get_value(bus, service_name, "/Dc/0/Voltage")
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _open_passive_can(interface: str) -> socket.socket:
    can_socket = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    can_socket.setblocking(False)
    can_socket.bind((interface,))
    return can_socket


def _drain_can(
    can_socket: socket.socket,
    core: VenusPublisherCore,
    timestamp: Callable[[], float] = time.time,
) -> int:
    accepted = 0
    while True:
        try:
            raw = can_socket.recv(CAN_FRAME_SIZE)
        except BlockingIOError:
            break
        try:
            core.apply_raw_frame(raw, timestamp=timestamp())
        except SocketCanFrameError:
            continue
        accepted += 1
    return accepted


def _qualify(
    can_socket: socket.socket,
    core: VenusPublisherCore,
    bus: Any,
    *,
    deadline: float,
    runtime_clock: RuntimeClock,
    vebus_service: str | None,
) -> None:
    while time.monotonic() < deadline:
        if _read_bms_instance(bus) != NO_BMS_CONTROL:
            raise RuntimeError("BMS control is not explicitly disabled (-255)")
        ready, _, _ = select.select([can_socket], [], [], 0.25)
        if ready:
            _drain_can(can_socket, core, runtime_clock.now)
        now = runtime_clock.now()
        core.step(
            timestamp=now,
            vebus_voltage_v=_read_vebus_voltage(bus, vebus_service),
        )
        if core.qualified:
            return
    missing = []
    if core.last_model is not None:
        missing = core.last_model["instantaneous_policy"][
            "missing_or_stale_control_fields"
        ]
    raise RuntimeError(f"CAN data did not qualify before deadline; missing={missing}")


def _add_paths(
    service: Any,
    initial: dict[str, Any],
    *,
    duration_seconds: float,
) -> set[str]:
    fixed = {
        "/Mgmt/ProcessName": __file__,
        "/Mgmt/ProcessVersion": f"{VERSION}-stage",
        "/Mgmt/Connection": "passive SocketCAN can0 (no-control stage)",
        "/DeviceInstance": DEVICE_INSTANCE,
        "/ProductId": 0xFFFF,
        "/ProductName": "Deye SE-F12-C",
        "/CustomName": "Deye SE-F12-C (staged; do not select)",
        "/Manufacturer": "Deye",
        "/FirmwareVersion": "shadow-stage",
        "/HardwareVersion": "SE-F12-C",
        "/Diagnostics/Stage/NoCanTransmit": 1,
        "/Diagnostics/Stage/NoDbusWrites": 1,
        "/Diagnostics/Stage/RequiresNoBmsControl": 1,
        "/Diagnostics/Stage/MeasurementOnly": 1,
        "/Diagnostics/Stage/DurationSeconds": duration_seconds,
    }
    for path, value in fixed.items():
        service.add_path(path, value)
    dynamic_paths: set[str] = set()
    for path, value in sorted(initial.items()):
        if path in fixed or path.startswith(STAGE_EXCLUDED_PREFIXES):
            continue
        service.add_path(path, value)
        dynamic_paths.add(path)
    return dynamic_paths


def run(arguments: argparse.Namespace) -> int:
    if not MINIMUM_DURATION_SECONDS <= arguments.duration_seconds <= MAXIMUM_DURATION_SECONDS:
        raise ValueError(
            f"duration must be between {MINIMUM_DURATION_SECONDS:g} and "
            f"{MAXIMUM_DURATION_SECONDS:g} seconds"
        )
    from .venus_bms_publisher import BATTERY_SERVICE_PREFIX, INTERFACE_PATTERN

    if not INTERFACE_PATTERN.match(arguments.interface):
        raise ValueError(f"not a valid CAN interface name: {arguments.interface!r}")
    if not arguments.service_name.startswith(BATTERY_SERVICE_PREFIX):
        raise ValueError(
            f"service name must start with {BATTERY_SERVICE_PREFIX!r}"
        )
    if not 0 <= arguments.device_instance <= 9999:
        raise ValueError(f"device instance out of range: {arguments.device_instance}")
    if not VELIB_PATH.is_dir():
        raise RuntimeError(f"Venus velib_python path not found: {VELIB_PATH}")

    sys.path.insert(0, str(VELIB_PATH))
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    from vedbus import VeDbusService

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    if _read_bms_instance(bus) != NO_BMS_CONTROL:
        raise RuntimeError("refusing to start unless BmsInstance is -255")

    vebus_service = getattr(arguments, "vebus_service", None) or discover_vebus_service(bus)
    can_socket = _open_passive_can(arguments.interface)
    core = VenusPublisherCore(
        policy_config=PolicyConfig(
            product_id=0xFFFF,
            device_instance=arguments.device_instance,
        )
    )
    runtime_clock = RuntimeClock()
    try:
        _qualify(
            can_socket,
            core,
            bus,
            vebus_service=vebus_service,
            deadline=time.monotonic() + arguments.qualification_seconds,
            runtime_clock=runtime_clock,
        )
        assert core.last_model is not None
        service = VeDbusService(arguments.service_name)
        dynamic_paths = _add_paths(
            service,
            core.last_model["paths"],
            duration_seconds=arguments.duration_seconds,
        )
        main_loop = GLib.MainLoop()
        started = time.monotonic()
        update_error: Exception | None = None

        def update() -> bool:
            nonlocal update_error
            try:
                _drain_can(can_socket, core, runtime_clock.now)
                if _read_bms_instance(bus) != NO_BMS_CONTROL:
                    raise RuntimeError("BmsInstance changed during staged observation")
                now = runtime_clock.now()
                model = core.step(
                    timestamp=now,
                    vebus_voltage_v=_read_vebus_voltage(bus, vebus_service),
                )
                with service as batch:
                    for path in dynamic_paths:
                        batch[path] = model["paths"][path]
                if time.monotonic() - started >= arguments.duration_seconds:
                    main_loop.quit()
                    return False
                return True
            except Exception as error:
                update_error = error
                logging.exception(
                    "staged publisher update failed; unregistering stale service"
                )
                main_loop.quit()
                return False

        GLib.io_add_watch(
            can_socket.fileno(),
            GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
            lambda *_: bool(
                _drain_can(can_socket, core, runtime_clock.now) >= 0
            ),
        )
        GLib.timeout_add(UPDATE_INTERVAL_MS, update)
        logging.info(
            "registered %s for a bounded %.1f-second no-control test",
            arguments.service_name,
            arguments.duration_seconds,
        )
        main_loop.run()
        if update_error is not None:
            raise RuntimeError("staged publisher update loop failed") from update_error
    finally:
        can_socket.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can0")
    parser.add_argument("--service-name", default=SERVICE_NAME)
    parser.add_argument("--device-instance", type=int, default=DEVICE_INSTANCE)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--qualification-seconds", type=float, default=10.0)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, arguments.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return run(arguments)
    except (OSError, RuntimeError, ValueError) as error:
        logging.error("staged publisher refused or failed: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
