"""Selectable Deye battery service for a supervised commissioning stage.

This adapter receives Deye telemetry from the already configured ``can0``
interface and publishes a native Venus battery service.  An optional guarded
transmitter can own the proven 0x305/0x307 keepalive pair, but only while an
explicit runtime arm file exists and the exact stock decoder executable is no
longer running.  It never changes a Venus setting, writes VE.Bus mode, or
starts/stops another service.

The internal commissioning diagnostics remain explicit because the effective
CVL policy has not yet been validated by Deye or Victron as a supported
integration.  The user-facing custom name is kept clean.  Selecting this
service causes dbus-systemcalc-py/DVCC to write its limits to VE.Bus and is
therefore a live control change requiring a separate approved procedure.
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
import select
import signal
import socket
import sys
import time
from typing import Any, Callable

from .can_keepalive import KeepaliveSnapshot, KeepaliveTransmitter
from .policy import PolicyConfig
from .venus_discovery import VebusLocator, read_vebus_voltage
from .venus_runtime import (
    CAN_FRAME_SIZE,
    RuntimeClock,
    SocketCanFrameError,
    VenusPublisherCore,
)
from .version import PROCESS_VERSION


SERVICE_NAME = "com.victronenergy.battery.deye_se_f12"
DEVICE_INSTANCE = 513
PRODUCT_ID = 0xFFFF
NO_BMS_CONTROL = -255
UPDATE_INTERVAL_MS = 250
VELIB_PATH = Path("/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
BMS_INSTANCE_PATH = "/Settings/SystemSetup/BmsInstance"

BATTERY_SERVICE_PREFIX = "com.victronenergy.battery."
# Linux IFNAMSIZ is 16 including the terminator, so 15 characters is the
# longest interface name the kernel will accept.
INTERFACE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,14}$")


def _unwrap(value: Any) -> Any:
    return getattr(value, "real", value) if not isinstance(value, bool) else value


def _get_value(bus: Any, service_name: str, path: str) -> Any:
    item = bus.get_object(service_name, path)
    return _unwrap(item.GetValue(dbus_interface="com.victronenergy.BusItem"))


def _read_number(bus: Any, service_name: str, path: str) -> float | None:
    try:
        value = _get_value(bus, service_name, path)
    except Exception:
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _read_bms_instance(bus: Any) -> int | None:
    value = _read_number(bus, "com.victronenergy.settings", BMS_INSTANCE_PATH)
    return int(value) if value is not None else None


def _read_vebus_voltage(bus: Any, locator: VebusLocator) -> float | None:
    return read_vebus_voltage(bus, locator, _read_number)


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
    vebus: VebusLocator,
) -> None:
    while time.monotonic() < deadline:
        ready, _, _ = select.select([can_socket], [], [], 0.25)
        if ready:
            _drain_can(can_socket, core, runtime_clock.now)
        now = runtime_clock.now()
        core.step(timestamp=now, vebus_voltage_v=_read_vebus_voltage(bus, vebus))
        if core.qualified:
            return
    missing: list[str] = []
    if core.last_model is not None:
        missing = core.last_model["instantaneous_policy"][
            "missing_or_stale_control_fields"
        ]
    raise RuntimeError(f"CAN data did not qualify before deadline; missing={missing}")


TX_DYNAMIC_PATHS = {
    "/Diagnostics/CanTx/Armed",
    "/Diagnostics/CanTx/StockDriverRunning",
    "/Diagnostics/CanTx/Owner",
    "/Diagnostics/CanTx/PairsSent",
    "/Diagnostics/CanTx/FramesSent",
    "/Diagnostics/CanTx/LastTxTimestamp",
    "/Diagnostics/CanTx/Errors",
    "/Diagnostics/CanTx/LastError",
}

PUBLISHER_DYNAMIC_PATHS = {
    "/UpdateIndex",
    "/Diagnostics/Publisher/Heartbeat",
}


def _tx_values(snapshot: KeepaliveSnapshot) -> dict[str, Any]:
    return {
        "/Diagnostics/CanTx/Armed": int(snapshot.armed),
        "/Diagnostics/CanTx/StockDriverRunning": int(snapshot.stock_driver_running),
        "/Diagnostics/CanTx/Owner": int(snapshot.owner),
        "/Diagnostics/CanTx/PairsSent": snapshot.pairs_sent,
        "/Diagnostics/CanTx/FramesSent": snapshot.frames_sent,
        "/Diagnostics/CanTx/LastTxTimestamp": snapshot.last_tx_timestamp,
        "/Diagnostics/CanTx/Errors": snapshot.tx_errors,
        "/Diagnostics/CanTx/LastError": snapshot.last_error,
    }


def _fixed_paths(
    config: PolicyConfig,
    *,
    can_tx_enabled: bool = False,
    interface: str = "can0",
    serial: str | None = None,
) -> dict[str, Any]:
    return {
        "/Mgmt/ProcessName": __file__,
        "/Mgmt/ProcessVersion": PROCESS_VERSION,
        "/Mgmt/Connection": (
            f"SocketCAN {interface}; guarded Deye keepalive ownership"
        ),
        "/DeviceInstance": DEVICE_INSTANCE,
        "/ProductId": PRODUCT_ID,
        "/ProductName": "Deye SE-F12-C",
        "/CustomName": "Deye SE-F12-C",
        "/Manufacturer": "Deye",
        # Device information, registered once at qualification and never
        # updated.  Keeping it out of the dynamic set means the update loop
        # can never change its D-Bus type, and an absent serial stays the
        # invalid value rather than appearing later as a different type.
        "/Serial": serial,
        "/FirmwareVersion": PROCESS_VERSION,
        "/HardwareVersion": "SE-F12-C",
        "/Capabilities/ChargeVoltageControl": 0,
        "/Diagnostics/Commissioning/Selectable": 1,
        "/Diagnostics/Commissioning/NoCanTransmit": int(not can_tx_enabled),
        "/Diagnostics/Commissioning/NoSettingsWrites": 1,
        "/Diagnostics/Commissioning/NoVebusModeWrites": 1,
        "/Diagnostics/CanTx/FeatureEnabled": int(can_tx_enabled),
        "/Diagnostics/CanTx/ArmFile": "/run/deye-virtual-battery/tx-armed",
        "/Diagnostics/Safety/CurrentLimitsFromFreshDeyeFrames": 1,
        "/Diagnostics/Safety/RuntimeClock": "monotonic-anchored-wall",
        "/Diagnostics/Safety/ChargeVoltageCeiling": (
            config.provisional_charge_ceiling_v
        ),
        "/Diagnostics/Safety/BlockedChargeVoltage": config.blocked_charge_cvl_v,
        "/Diagnostics/Safety/AlarmFlagsControlLimits": 0,
        "/Diagnostics/Profile/Supported": "deye_native,victron_can",
    }


def _add_paths(
    service: Any,
    initial: dict[str, Any],
    *,
    config: PolicyConfig,
    can_tx_enabled: bool = False,
    transmitter_snapshot: KeepaliveSnapshot | None = None,
    interface: str = "can0",
    serial: str | None = None,
) -> set[str]:
    fixed = _fixed_paths(
        config, can_tx_enabled=can_tx_enabled, interface=interface, serial=serial
    )
    for path, value in fixed.items():
        service.add_path(path, value)
    dynamic_paths: set[str] = set()
    for path, value in sorted(initial.items()):
        if path in fixed:
            continue
        service.add_path(path, value)
        dynamic_paths.add(path)
    service.add_path("/Diagnostics/Commissioning/Selected", 0)
    dynamic_paths.add("/Diagnostics/Commissioning/Selected")
    service.add_path("/Diagnostics/Publisher/Heartbeat", 0)
    service.add_path("/UpdateIndex", 0)
    dynamic_paths.update(PUBLISHER_DYNAMIC_PATHS)
    if transmitter_snapshot is None:
        transmitter_snapshot = KeepaliveSnapshot(
            feature_enabled=can_tx_enabled,
            armed=False,
            stock_driver_running=True,
            owner=False,
            pairs_sent=0,
            frames_sent=0,
            last_tx_timestamp=None,
            tx_errors=0,
            last_error="",
        )
    for path, value in _tx_values(transmitter_snapshot).items():
        service.add_path(path, value)
        dynamic_paths.add(path)
    return dynamic_paths


def _validate(arguments: argparse.Namespace) -> None:
    """Reject a configuration that cannot be published safely.

    The values are configurable because the CAN port and the D-Bus instance
    differ between installations -- BMS-Can is ``can0`` on some GX models and
    ``can1`` on others.  They are still validated, because a malformed service
    name or an out-of-range instance would either fail to register or collide
    with another battery already on the bus.
    """
    if not INTERFACE_PATTERN.match(arguments.interface):
        raise ValueError(f"not a valid CAN interface name: {arguments.interface!r}")
    if not arguments.service_name.startswith(BATTERY_SERVICE_PREFIX):
        raise ValueError(
            f"service name must start with {BATTERY_SERVICE_PREFIX!r}: "
            f"{arguments.service_name!r}"
        )
    suffix = arguments.service_name[len(BATTERY_SERVICE_PREFIX):]
    if not suffix or not all(part.isidentifier() for part in suffix.split(".")):
        raise ValueError(f"invalid D-Bus service suffix: {suffix!r}")
    if not 0 <= arguments.device_instance <= 9999:
        raise ValueError(
            f"device instance out of range: {arguments.device_instance}"
        )
    if arguments.vebus_service is not None and not arguments.vebus_service.startswith(
        "com.victronenergy.vebus."
    ):
        raise ValueError(
            f"not a VE.Bus service name: {arguments.vebus_service!r}"
        )
    if not 5.0 <= arguments.qualification_seconds <= 60.0:
        raise ValueError("qualification must be between 5 and 60 seconds")
    if arguments.duration_seconds is not None and not (
        15.0 <= arguments.duration_seconds <= 600.0
    ):
        raise ValueError("duration must be between 15 and 600 seconds")


def run(arguments: argparse.Namespace) -> int:
    _validate(arguments)
    if not VELIB_PATH.is_dir():
        raise RuntimeError(f"Venus velib_python path not found: {VELIB_PATH}")

    sys.path.insert(0, str(VELIB_PATH))
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    from vedbus import VeDbusService

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    if arguments.require_no_bms_control and _read_bms_instance(bus) != NO_BMS_CONTROL:
        raise RuntimeError("BmsInstance must be -255 for unselected discovery")
    can_socket = _open_passive_can(arguments.interface)
    transmitter = KeepaliveTransmitter(
        interface=arguments.interface,
        feature_enabled=arguments.allow_can_transmit,
    )
    transmitter.start()
    vebus = VebusLocator(explicit=arguments.vebus_service, clock=time.monotonic)
    config = PolicyConfig(
        product_id=PRODUCT_ID, device_instance=arguments.device_instance
    )
    core = VenusPublisherCore(policy_config=config)
    runtime_clock = RuntimeClock()
    service = None
    try:
        _qualify(
            can_socket,
            core,
            bus,
            deadline=time.monotonic() + arguments.qualification_seconds,
            runtime_clock=runtime_clock,
            vebus=vebus,
        )
        assert core.last_model is not None
        service = VeDbusService(arguments.service_name, register=False)
        dynamic_paths = _add_paths(
            service,
            core.last_model["paths"],
            config=config,
            can_tx_enabled=arguments.allow_can_transmit,
            transmitter_snapshot=transmitter.snapshot(),
            interface=arguments.interface,
            serial=core.last_model["paths"].get("/Serial"),
        )
        service.register()
        main_loop = GLib.MainLoop()
        started = time.monotonic()
        heartbeat = 0
        update_index = 0
        update_error: Exception | None = None

        def update() -> bool:
            nonlocal heartbeat, update_index, update_error
            try:
                _drain_can(can_socket, core, runtime_clock.now)
                now = runtime_clock.now()
                model = core.step(
                    timestamp=now,
                    vebus_voltage_v=_read_vebus_voltage(bus, vebus),
                )
                bms_instance = _read_bms_instance(bus)
                if arguments.require_no_bms_control and bms_instance != NO_BMS_CONTROL:
                    raise RuntimeError(
                        "BmsInstance changed during unselected discovery"
                    )
                selected = int(bms_instance == arguments.device_instance)
                tx_values = _tx_values(transmitter.snapshot())
                heartbeat += 1
                update_index = (update_index + 1) % 256
                with service as batch:
                    for path in dynamic_paths:
                        if path == "/Diagnostics/Commissioning/Selected":
                            batch[path] = selected
                        elif path == "/Diagnostics/Publisher/Heartbeat":
                            batch[path] = heartbeat
                        elif path == "/UpdateIndex":
                            batch[path] = update_index
                        elif path in TX_DYNAMIC_PATHS:
                            batch[path] = tx_values[path]
                        else:
                            batch[path] = model["paths"][path]
                if (
                    arguments.duration_seconds is not None
                    and time.monotonic() - started >= arguments.duration_seconds
                ):
                    main_loop.quit()
                    return False
                return True
            except Exception as error:
                update_error = error
                logging.exception(
                    "publisher update failed; unregistering instead of leaving stale D-Bus data"
                )
                main_loop.quit()
                return False

        def stop(*_: Any) -> None:
            logging.warning("termination requested; unregistering battery service")
            main_loop.quit()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        GLib.io_add_watch(
            can_socket.fileno(),
            GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
            lambda *_: bool(
                _drain_can(can_socket, core, runtime_clock.now) >= 0
            ),
        )
        GLib.timeout_add(UPDATE_INTERVAL_MS, update)
        logging.info(
            "registered selectable commissioning service %s; BmsInstance=%s",
            arguments.service_name,
            _read_bms_instance(bus),
        )
        main_loop.run()
        if update_error is not None:
            raise RuntimeError("publisher update loop failed") from update_error
    finally:
        # Dropping the last VeDbusService reference unregisters it.  This code
        # does not change BmsInstance: rollback must select No BMS control
        # before a supervised service is intentionally stopped.
        service = None
        transmitter.stop()
        can_socket.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can0")
    parser.add_argument("--service-name", default=SERVICE_NAME)
    parser.add_argument("--device-instance", type=int, default=DEVICE_INSTANCE)
    parser.add_argument(
        "--vebus-service",
        default=None,
        help=(
            "VE.Bus D-Bus service used for the DC voltage cross-check. "
            "Discovered automatically when omitted."
        ),
    )
    parser.add_argument("--qualification-seconds", type=float, default=10.0)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--require-no-bms-control", action="store_true")
    parser.add_argument("--allow-can-transmit", action="store_true")
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
        logging.error("commissioning publisher refused or failed: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
