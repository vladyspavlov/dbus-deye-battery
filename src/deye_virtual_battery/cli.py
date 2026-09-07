"""Command-line interface for replaying and following local CAN logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from .cache import VirtualBatteryCache
from .candump import CandumpParseError, parse_candump_line
from .dbus_model import build_mock_dbus_model
from .simulation import simulate_recordings, simulate_stateful_recordings


def ingest_line(cache: VirtualBatteryCache, line: str, source: str, number: int) -> None:
    if not line.strip():
        return
    try:
        frame = parse_candump_line(line)
    except CandumpParseError as error:
        cache.decode_errors += 1
        cache.errors.append({"source": source, "line": number, "error": str(error)})
        cache.errors = cache.errors[-100:]
        return
    cache.apply(frame)


def replay(paths: list[Path], capacity: float) -> VirtualBatteryCache:
    cache = VirtualBatteryCache(installed_capacity_ah=capacity)
    for path in paths:
        with path.open(encoding="ascii", errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                ingest_line(cache, line, str(path), line_number)
    return cache


def latest_capture(root: Path) -> Path:
    candidates = list(root.glob("**/can0-*.log"))
    if not candidates:
        raise FileNotFoundError(f"no capture logs under {root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def print_human(snapshot: dict[str, object]) -> None:
    fields = snapshot["fields"]
    health = snapshot["health"]
    statistics = snapshot["statistics"]
    assert isinstance(fields, dict)
    assert isinstance(health, dict)
    assert isinstance(statistics, dict)

    def value(name: str) -> str:
        entry = fields.get(name)
        if not isinstance(entry, dict):
            return "unavailable"
        measured = entry.get("effective_value")
        unit = entry.get("unit") or ""
        freshness = entry.get("freshness")
        return f"{measured} {unit} ({freshness})".strip()

    print("Deye SE-F12-C PC-only shadow state")
    print(f"frames: {statistics.get('total_frames')}, decode errors: {statistics.get('decode_errors')}")
    print(
        f"BMS protocol profile: {health.get('bms_protocol_profile')}"
        f" (0x356 sign factor {health.get('current_sign_factor')})"
    )
    print(f"voltage: {value('battery.voltage')}")
    print(f"current: {value('battery.current')}")
    print(f"SOC: {value('battery.soc')}")
    print(f"CVL: {value('limits.max_charge_voltage')}")
    print(f"CCL: {value('limits.max_charge_current')}")
    print(f"array CCL: {value('limits.array_max_charge_current')}")
    print(f"DCL: {value('limits.max_discharge_current')}")
    print(f"manufacturer: {value('identity.manufacturer')}")
    print(f"installed capacity: {value('battery.installed_capacity')}")
    print(f"charge MOS closed: {value('mos.charge_closed')}")
    print(f"discharge MOS closed: {value('mos.discharge_closed')}")
    print(f"operation mode: {value('system.operation_mode')}")
    print(f"system sub-state: {value('system.substate_raw')}")
    print(f"charge path: {health.get('charge_path_state')}")
    print(f"charge-signal disagreement: {health.get('charge_signal_disagreement')}")
    print(f"active V3.3 pack condition: {health.get('v33_pack_condition_active')}")
    print(f"active V3.3 system condition: {health.get('v33_system_condition_active')}")
    print(f"CCL pulses without measured charge: {health.get('ccl_pulse_without_measured_charge_count')}")
    print(f"356/150 voltage delta: {health.get('voltage_356_vs_150_delta_v')} V")
    print(f"high-CVL/zero-CCL warning: {health.get('high_cvl_zero_ccl')}")
    print(f"stock Victron LG identity hazard: {health.get('stock_victron_identity_hazard')}")
    print(f"wire identity claims Pylontech: {health.get('wire_identity_claims_pylontech')}")
    print(f"current sign conflict: {health.get('current_sign_convention_conflict')}")
    print("CAN TX: disabled; D-Bus publication: disabled")


def command_replay(arguments: argparse.Namespace) -> int:
    cache = replay(arguments.paths, arguments.capacity_ah)
    snapshot = cache.snapshot()
    if arguments.json:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    else:
        print_human(snapshot)
    return 1 if snapshot["statistics"]["decode_errors"] else 0


def command_model(arguments: argparse.Namespace) -> int:
    cache = replay(arguments.paths, arguments.capacity_ah)
    snapshot = cache.snapshot()
    model = build_mock_dbus_model(snapshot, vebus_voltage_v=arguments.vebus_voltage)
    print(json.dumps(model, indent=2, sort_keys=True))
    return 1 if snapshot["statistics"]["decode_errors"] else 0


def command_simulate(arguments: argparse.Namespace) -> int:
    result = simulate_recordings(
        arguments.can,
        arguments.vebus,
        capacity_ah=arguments.capacity_ah,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_simulate_venus(arguments: argparse.Namespace) -> int:
    result = simulate_stateful_recordings(
        arguments.can,
        arguments.vebus,
        capacity_ah=arguments.capacity_ah,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_watch(arguments: argparse.Namespace) -> int:
    path = arguments.path or latest_capture(arguments.capture_root)
    cache = VirtualBatteryCache(installed_capacity_ah=arguments.capacity_ah)
    print(f"following local file only: {path}", file=sys.stderr)
    with path.open(encoding="ascii", errors="replace") as stream:
        line_number = 0
        last_print = 0.0
        while True:
            line = stream.readline()
            if line:
                line_number += 1
                ingest_line(cache, line, str(path), line_number)
            else:
                now = time.monotonic()
                if now - last_print >= arguments.interval:
                    print(json.dumps(cache.snapshot(at=time.time()), sort_keys=True), flush=True)
                    last_print = now
                time.sleep(0.1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser("replay", help="decode one or more completed/local logs")
    replay_parser.add_argument("paths", type=Path, nargs="+")
    replay_parser.add_argument("--capacity-ah", type=float, default=230.0)
    replay_parser.add_argument("--json", action="store_true")
    replay_parser.set_defaults(function=command_replay)

    model_parser = subparsers.add_parser(
        "model",
        help="build a PC-only mock Venus battery service from completed CAN logs",
    )
    model_parser.add_argument("paths", type=Path, nargs="+")
    model_parser.add_argument("--capacity-ah", type=float, default=230.0)
    model_parser.add_argument(
        "--vebus-voltage",
        type=float,
        help="optional local test observation; never reads or writes live D-Bus",
    )
    model_parser.set_defaults(function=command_model)

    simulate_parser = subparsers.add_parser(
        "simulate",
        help="replay local CAN and VE.Bus capture files through the shadow policy",
    )
    simulate_parser.add_argument("--can", type=Path, nargs="+", required=True)
    simulate_parser.add_argument("--vebus", type=Path, nargs="+", required=True)
    simulate_parser.add_argument("--capacity-ah", type=float, default=230.0)
    simulate_parser.set_defaults(function=command_simulate)

    simulate_venus_parser = subparsers.add_parser(
        "simulate-venus",
        help="offline stateful alarm/CAN-loss/mock-systemcalc replay",
    )
    simulate_venus_parser.add_argument("--can", type=Path, nargs="+", required=True)
    simulate_venus_parser.add_argument("--vebus", type=Path, nargs="+", required=True)
    simulate_venus_parser.add_argument("--capacity-ah", type=float, default=230.0)
    simulate_venus_parser.set_defaults(function=command_simulate_venus)

    watch_parser = subparsers.add_parser("watch", help="follow a local capture without CAN or D-Bus access")
    watch_parser.add_argument("path", type=Path, nargs="?")
    watch_parser.add_argument("--capture-root", type=Path, default=Path("captures/live"))
    watch_parser.add_argument("--capacity-ah", type=float, default=230.0)
    watch_parser.add_argument("--interval", type=float, default=5.0)
    watch_parser.set_defaults(function=command_watch)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        return arguments.function(arguments)
    except (FileNotFoundError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
