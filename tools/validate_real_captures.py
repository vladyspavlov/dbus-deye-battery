#!/usr/bin/env python3
"""Validate the Deye/Victron candidate against preserved passive CAN data.

This tool only reads candump text.  It does not open SocketCAN, contact a GX,
publish D-Bus paths, or transmit CAN frames.  Use ``-`` to read a capture from
standard input without storing another local copy.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import CandumpParseError, parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.decoder import DecodeError, DeyeDecoder


EXPECTED_KEEPALIVES = {
    0x305: bytes.fromhex("0000000000000000"),
    0x307: bytes.fromhex("1234567856494300"),
}
# Frames every profile must supply, plus the extra frames each BMS-side
# protocol profile is expected to add.  The victronCAN profile replaces the
# Deye 0x359 fault frame with Victron's standard 0x35A/0x35F pair.
REQUIRED_COMMON_IDS = {0x351, 0x355, 0x356, 0x35E}
REQUIRED_DEYE_NATIVE_IDS = REQUIRED_COMMON_IDS | {0x359}
REQUIRED_VICTRON_CAN_IDS = REQUIRED_COMMON_IDS | {0x35A, 0x35F}
REQUIRED_DEYE_IDS = REQUIRED_DEYE_NATIVE_IDS
POLICY_SAMPLE_IDS = {0x351, 0x359, 0x35A, 0x110, 0x371}


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _value(snapshot: dict[str, Any], name: str) -> Any:
    state = snapshot.get("fields", {}).get(name)
    return state.get("effective_value") if isinstance(state, dict) else None


def _source(path: Path) -> Any:
    if str(path) == "-":
        return nullcontext(sys.stdin)
    return path.open(encoding="ascii", errors="replace")


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def validate(paths: Iterable[Path], *, capacity_ah: float = 230.0) -> dict[str, Any]:
    paths = list(paths)
    decoder = DeyeDecoder()
    identifiers: Counter[int] = Counter()
    directions: Counter[str] = Counter()
    keepalive_bad: list[dict[str, Any]] = []
    keepalive_times: dict[int, list[float]] = defaultdict(list)
    limit_times: list[float] = []
    non_frame_lines = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    raw_cvl: set[float] = set()
    raw_ccl: set[float] = set()
    raw_dcl: set[float] = set()
    capacities: set[float] = set()
    active_conditions: set[str] = set()
    fault_levels: set[str] = set()
    cell_min: tuple[float, float] | None = None
    cell_max: tuple[float, float] | None = None
    high_cell_events: list[dict[str, Any]] = []
    policy_samples = 0
    candidate_discharge_stops = 0
    candidate_direct_off_actions = 0
    candidate_limit_sets: Counter[tuple[float | None, float | None, float | None]] = Counter()
    candidate_alarm_samples: Counter[tuple[str, int]] = Counter()
    discharge_mos_states: set[bool] = set()
    total_frames = 0
    decoded_frames = 0
    decode_errors = 0
    unknown_frames = 0
    final_paths: dict[str, Any] = {}
    final_paths_timestamp: float | None = None
    observed_profiles: set[str] = set()

    for path in paths:
        cache = VirtualBatteryCache(installed_capacity_ah=capacity_ah)
        source_last_timestamp: float | None = None
        last_policy_payload: dict[int, bytes] = {}
        source_policy_ever_ready = False
        with _source(path) as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    frame = parse_candump_line(line)
                except CandumpParseError:
                    non_frame_lines += 1
                    continue

                identifiers[frame.can_id] += 1
                directions[frame.direction or "unknown"] += 1
                first_timestamp = (
                    frame.timestamp
                    if first_timestamp is None
                    else min(first_timestamp, frame.timestamp)
                )
                last_timestamp = (
                    frame.timestamp
                    if last_timestamp is None
                    else max(last_timestamp, frame.timestamp)
                )
                source_last_timestamp = (
                    frame.timestamp
                    if source_last_timestamp is None
                    else max(source_last_timestamp, frame.timestamp)
                )
                if frame.can_id in EXPECTED_KEEPALIVES:
                    keepalive_times[frame.can_id].append(frame.timestamp)
                    if frame.payload != EXPECTED_KEEPALIVES[frame.can_id]:
                        keepalive_bad.append(
                            {
                                "source": str(path),
                                "line": line_number,
                                "can_id": f"0x{frame.can_id:03X}",
                                "payload": frame.payload.hex().upper(),
                            }
                        )
                if frame.can_id == 0x351:
                    limit_times.append(frame.timestamp)

                cache.apply(frame)
                try:
                    decoded = decoder.decode(frame)
                except DecodeError:
                    continue
                sample_policy = (
                    not source_policy_ever_ready
                    or (
                        frame.can_id in POLICY_SAMPLE_IDS
                        and last_policy_payload.get(frame.can_id) != frame.payload
                    )
                )
                if frame.can_id in POLICY_SAMPLE_IDS:
                    last_policy_payload[frame.can_id] = frame.payload
                for field in decoded:
                    if not field.valid:
                        continue
                    if field.name == "limits.max_charge_voltage":
                        raw_cvl.add(float(field.value))
                    elif field.name == "limits.max_charge_current":
                        raw_ccl.add(float(field.value))
                    elif field.name == "limits.max_discharge_current":
                        raw_dcl.add(float(field.value))
                    elif field.name == "battery.installed_capacity":
                        capacities.add(float(field.value))
                    elif field.name in {
                        "pack.active_v33_conditions",
                        "alarms.active_v33_conditions",
                    }:
                        active_conditions.update(field.value)
                    elif field.name == "system.fault_level":
                        fault_levels.add(str(field.value))
                    elif field.name == "mos.discharge_closed":
                        discharge_mos_states.add(bool(field.value))
                    elif field.name.startswith("cells.min_voltage_"):
                        candidate = float(field.value)
                        if cell_min is None or candidate < cell_min[0]:
                            cell_min = (candidate, frame.timestamp)
                    elif field.name.startswith("cells.max_voltage_"):
                        candidate = float(field.value)
                        if cell_max is None or candidate > cell_max[0]:
                            cell_max = (candidate, frame.timestamp)
                        if candidate > 3.65:
                            sample_policy = True

                if not sample_policy:
                    continue
                snapshot = cache.snapshot(at=frame.timestamp)
                model = build_mock_dbus_model(snapshot)
                policy = model["policy"]
                if not policy["critical_data_ready"]:
                    continue
                source_policy_ever_ready = True
                policy_samples += 1
                limits = policy["effective_limits"]
                candidate_limit_sets[
                    (limits["cvl_v"], limits["ccl_a"], limits["dcl_a"])
                ] += 1
                candidate_discharge_stops += int(
                    policy["permissions"]["would_request_discharge_stop"]
                )
                candidate_direct_off_actions += int(
                    policy["permissions"]["would_directly_switch_inverter_off"]
                )
                for alarm_path, level in policy["standard_alarms"].items():
                    if isinstance(level, int) and level > 0:
                        candidate_alarm_samples[(alarm_path, level)] += 1
                if (
                    frame.can_id in {0x200, 0x361}
                    and
                    policy["standard_alarms"].get("/Alarms/HighCellVoltage") == 2
                    and len(high_cell_events) < 100
                ):
                    high_cell_events.append(
                        {
                            "timestamp": frame.timestamp,
                            "time_utc": _iso(frame.timestamp),
                            "max_cell_voltage_v": policy["diagnostics"][
                                "maximum_cell_voltage_v"
                            ],
                            "pack_voltage_v": _value(snapshot, "battery.voltage"),
                            "raw_ccl_a": policy["raw_limits"]["ccl_a"],
                            "charge_mos_closed": policy["diagnostics"][
                                "charge_mos_closed"
                            ],
                            "effective_ccl_a": limits["ccl_a"],
                            "effective_dcl_a": limits["dcl_a"],
                            "allow_discharge": policy["permissions"][
                                "allow_discharge"
                            ],
                        }
                    )
        observed_profiles.update(
            event["current"]
            for event in cache.protocol_events
            if event["field"] == "protocol.bms_profile"
        )
        if source_last_timestamp is not None:
            observed_profiles.add(cache.profile_detector.resolve(source_last_timestamp))
        total_frames += cache.total_frames
        decoded_frames += cache.decoded_frames
        decode_errors += cache.decode_errors
        unknown_frames += cache.unknown_frames
        if source_last_timestamp is not None:
            source_model = build_mock_dbus_model(
                cache.snapshot(at=source_last_timestamp)
            )
            if (
                final_paths_timestamp is None
                or source_last_timestamp > final_paths_timestamp
            ):
                final_paths_timestamp = source_last_timestamp
                final_paths = source_model["paths"]

    def gap_summary(timestamps: list[float]) -> dict[str, Any]:
        ordered = sorted(timestamps)
        gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:])]
        continuous = [gap for gap in gaps if gap <= 3.0]
        outages = [gap for gap in gaps if gap > 3.0]
        return {
            "maximum_continuous_gap_seconds": max(continuous, default=None),
            "gaps_over_3_seconds": len(outages),
            "largest_gap_seconds": max(gaps, default=None),
        }

    observed_ids = set(identifiers)
    profile = ",".join(sorted(observed_profiles)) or "unknown"
    checks = [
        _check(
            "all recognized frames decode",
            decode_errors == 0,
            f"decode_errors={decode_errors}",
        ),
        _check(
            "no unsupported CAN identifiers",
            unknown_frames == 0,
            f"unknown_or_wrong_direction_frames={unknown_frames}",
        ),
        _check(
            "required frame family present for an observed BMS profile",
            REQUIRED_DEYE_NATIVE_IDS <= observed_ids
            or REQUIRED_VICTRON_CAN_IDS <= observed_ids,
            "profile=" + profile + "; missing_deye_native=" + ",".join(
                f"0x{can_id:03X}"
                for can_id in sorted(REQUIRED_DEYE_NATIVE_IDS - observed_ids)
            ) + "; missing_victron_can=" + ",".join(
                f"0x{can_id:03X}"
                for can_id in sorted(REQUIRED_VICTRON_CAN_IDS - observed_ids)
            ),
        ),
        _check(
            "Victron keepalive payloads exact",
            not keepalive_bad and EXPECTED_KEEPALIVES.keys() <= observed_ids,
            f"bad_payloads={len(keepalive_bad)}",
        ),
        _check(
            "Deye current limits never decode negative",
            all(value >= 0 for value in raw_ccl | raw_dcl),
            f"CCL range={min(raw_ccl, default=None)}..{max(raw_ccl, default=None)} A; "
            f"DCL range={min(raw_dcl, default=None)}..{max(raw_dcl, default=None)} A",
        ),
        _check(
            "Deye nominal capacity is consistently 230 Ah",
            capacities == {230.0},
            f"observed={sorted(capacities)}",
        ),
        _check(
            "candidate never writes inverter-off action",
            candidate_direct_off_actions == 0,
            f"samples={policy_samples}, actions={candidate_direct_off_actions}",
        ),
        _check(
            "candidate did not synthesize a discharge stop in observed states",
            candidate_discharge_stops == 0,
            f"samples={policy_samples}, discharge_stops={candidate_discharge_stops}",
        ),
        _check(
            "capacity and unavailable cell IDs map to Victron D-Bus semantics",
            final_paths.get("/InstalledCapacity") == 230.0
            and final_paths.get("/Capacity") == 230.0
            and final_paths.get("/System/MaxVoltageCellId") is None
            and final_paths.get("/System/MinVoltageCellId") is None,
            "Installed/Capacity="
            f"{final_paths.get('/InstalledCapacity')}/{final_paths.get('/Capacity')} Ah; "
            "cell IDs="
            f"{final_paths.get('/System/MinVoltageCellId')}/"
            f"{final_paths.get('/System/MaxVoltageCellId')}",
        ),
    ]

    return {
        "mode": "offline-passive-capture-validation",
        "action_taken": False,
        "remote_contacted": False,
        "registered_on_dbus": False,
        "transmitted_can": False,
        "result": "pass" if all(check["passed"] for check in checks) else "fail",
        "sources": [str(path) for path in paths],
        "time_range": {
            "first_timestamp": first_timestamp,
            "first_utc": _iso(first_timestamp),
            "last_timestamp": last_timestamp,
            "last_utc": _iso(last_timestamp),
        },
        "observed_bms_profiles": sorted(observed_profiles),
        "statistics": {
            "total_frames": total_frames,
            "decoded_frames": decoded_frames,
            "decode_errors": decode_errors,
            "unknown_or_wrong_direction_frames": unknown_frames,
            "non_frame_lines_ignored": non_frame_lines,
            "directions": dict(sorted(directions.items())),
            "identifiers": {
                f"0x{can_id:03X}": count
                for can_id, count in sorted(identifiers.items())
            },
        },
        "timing": {
            "0x305": gap_summary(keepalive_times[0x305]),
            "0x307": gap_summary(keepalive_times[0x307]),
            "0x351": gap_summary(limit_times),
        },
        "observations": {
            "raw_cvl_v": sorted(raw_cvl),
            "raw_ccl_range_a": [min(raw_ccl, default=None), max(raw_ccl, default=None)],
            "raw_dcl_range_a": [min(raw_dcl, default=None), max(raw_dcl, default=None)],
            "nominal_capacities_ah": sorted(capacities),
            "active_deye_conditions": sorted(active_conditions),
            "system_fault_levels": sorted(fault_levels),
            "minimum_cell_voltage": (
                {"value_v": cell_min[0], "time_utc": _iso(cell_min[1])}
                if cell_min
                else None
            ),
            "maximum_cell_voltage": (
                {"value_v": cell_max[0], "time_utc": _iso(cell_max[1])}
                if cell_max
                else None
            ),
            "high_cell_events": high_cell_events,
        },
        "candidate_policy": {
            "samples": policy_samples,
            "effective_limit_sets": [
                {"cvl_v": values[0], "ccl_a": values[1], "dcl_a": values[2], "samples": count}
                for values, count in candidate_limit_sets.most_common()
            ],
            "active_alarm_samples": [
                {"path": values[0], "level": values[1], "samples": count}
                for values, count in candidate_alarm_samples.most_common()
            ],
            "discharge_stop_samples": candidate_discharge_stops,
            "direct_inverter_off_actions": candidate_direct_off_actions,
        },
        "coverage": {
            "real_active_alarm_or_warning_bits_observed": bool(active_conditions),
            "real_zero_dcl_observed": 0.0 in raw_dcl,
            "real_discharge_mos_open_observed": False in discharge_mos_states,
            "note": (
                "No live Deye warning/protection bit or zero-DCL state occurs in these captures; "
                "those branches remain protocol-vector/synthetic-test coverage, not real-event validation."
            ),
        },
        "checks": checks,
        "keepalive_payload_errors": keepalive_bad,
    }


def default_paths() -> list[Path]:
    """Fall back to the bundled sample recordings when none are given."""
    data = Path(__file__).resolve().parents[1] / "tests" / "data"
    return sorted(data.glob("*.log")) + sorted((data / "private").glob("*.log"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--capacity-ah", type=float, default=230.0)
    parser.add_argument("--compact", action="store_true")
    arguments = parser.parse_args(argv)
    paths = arguments.paths or default_paths()
    if not paths:
        parser.error("no capture files found")
    result = validate(paths, capacity_ah=arguments.capacity_ah)
    print(json.dumps(result, indent=None if arguments.compact else 2, sort_keys=True))
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
