"""Replay local CAN and VE.Bus recordings through the shadow battery policy."""

from __future__ import annotations

import json
import heapq
from pathlib import Path
from typing import Any, Iterator

from .cache import VirtualBatteryCache
from .candump import CandumpParseError, CanFrame, parse_candump_line
from .dbus_model import build_mock_dbus_model
from .policy import PolicyConfig
from .stateful import StatefulConfig, StatefulShadow
from .venus_simulator import (
    CURRENT_NO_BMS_SCENARIO,
    HYPOTHETICAL_VIRTUAL_BMS_SCENARIO,
    simulate_systemcalc,
)


def simulate_recordings(
    can_paths: list[Path],
    vebus_paths: list[Path],
    *,
    capacity_ah: float = 230.0,
    config: PolicyConfig | None = None,
) -> dict[str, Any]:
    """Correlate every recorded VE.Bus voltage with the latest CAN state."""

    config = config or PolicyConfig()
    cache = VirtualBatteryCache(installed_capacity_ah=capacity_ah)
    frames = _iter_can_frames(can_paths)
    current_frame = next(frames, None)
    samples = 0
    mismatch_samples = 0
    charge_inhibit_samples = 0
    discharge_stop_samples = 0
    standard_high_voltage_samples = 0
    maximum_absolute_delta: float | None = None
    maximum_vebus_voltage: float | None = None
    most_severe_event: dict[str, Any] | None = None
    first_mismatch: dict[str, Any] | None = None
    last_mismatch: dict[str, Any] | None = None

    for event in _load_vebus_voltage_events(vebus_paths):
        timestamp = event["timestamp"]
        while current_frame is not None and current_frame.timestamp <= timestamp:
            cache.apply(current_frame)
            current_frame = next(frames, None)

        snapshot = cache.snapshot(at=timestamp)
        model = build_mock_dbus_model(
            snapshot,
            vebus_voltage_v=event["value"],
            config=config,
        )
        policy = model["policy"]
        observation = policy["voltage_observations"]
        permissions = policy["permissions"]
        samples += 1
        maximum_vebus_voltage = _max_optional(maximum_vebus_voltage, event["value"])
        delta = observation["vebus_minus_deye_v"]
        if isinstance(delta, (int, float)):
            maximum_absolute_delta = _max_optional(maximum_absolute_delta, abs(delta))
        if permissions["allow_charge"] is False:
            charge_inhibit_samples += 1
        if permissions["would_request_discharge_stop"]:
            discharge_stop_samples += 1
        if policy["standard_alarms"]["/Alarms/HighVoltage"]:
            standard_high_voltage_samples += 1
        if not observation["mismatch"]:
            continue

        mismatch_samples += 1
        record = {
            "timestamp": timestamp,
            "vebus_voltage_v": event["value"],
            "deye_voltage_v": observation["deye_pack_voltage_v"],
            "vebus_minus_deye_v": delta,
            "integration_alarm_level": policy["custom_alarms"][
                "/Diagnostics/Alarms/VoltageDisagreement"
            ],
            "effective_cvl_v": policy["effective_limits"]["cvl_v"],
            "effective_ccl_a": policy["effective_limits"]["ccl_a"],
            "effective_dcl_a": policy["effective_limits"]["dcl_a"],
            "would_request_discharge_stop": permissions["would_request_discharge_stop"],
            "would_directly_switch_inverter_off": permissions[
                "would_directly_switch_inverter_off"
            ],
            "standard_high_voltage_alarm": policy["standard_alarms"][
                "/Alarms/HighVoltage"
            ],
        }
        first_mismatch = first_mismatch or record
        last_mismatch = record
        if most_severe_event is None or _event_rank(record) > _event_rank(most_severe_event):
            most_severe_event = record

    while current_frame is not None:
        cache.apply(current_frame)
        current_frame = next(frames, None)

    final_snapshot = cache.snapshot()
    final_model = build_mock_dbus_model(final_snapshot, config=config)
    return {
        "mode": "pc-only-recording-simulation",
        "action_taken": False,
        "registered_on_dbus": False,
        "transmitted_can": False,
        "remote_contacted": False,
        "can_paths": [str(path) for path in can_paths],
        "vebus_paths": [str(path) for path in vebus_paths],
        "samples": samples,
        "mismatch_samples": mismatch_samples,
        "charge_inhibit_samples": charge_inhibit_samples,
        "discharge_stop_samples": discharge_stop_samples,
        "standard_high_voltage_samples": standard_high_voltage_samples,
        "maximum_vebus_voltage_v": maximum_vebus_voltage,
        "maximum_absolute_voltage_delta_v": maximum_absolute_delta,
        "first_mismatch": first_mismatch,
        "last_mismatch": last_mismatch,
        "most_severe_mismatch": most_severe_event,
        "final_model": final_model,
        "can_statistics": final_snapshot["statistics"],
    }


def simulate_stateful_recordings(
    can_paths: list[Path],
    vebus_paths: list[Path],
    *,
    capacity_ah: float = 230.0,
    policy_config: PolicyConfig | None = None,
    stateful_config: StatefulConfig | None = None,
) -> dict[str, Any]:
    """Replay local files through lifecycle, alarms, and two Venus scenarios."""

    cache = VirtualBatteryCache(installed_capacity_ah=capacity_ah)
    frames = _iter_can_frames(can_paths)
    current_frame = next(frames, None)
    shadow = StatefulShadow(
        policy_config=policy_config or PolicyConfig(),
        stateful_config=stateful_config or StatefulConfig(),
    )
    samples = 0
    lifecycle_counts: dict[str, int] = {}
    current_no_bms_lost_samples = 0
    hypothetical_selected_bms_lost_samples = 0
    voltage_alarm_samples = 0
    last_stateful: dict[str, Any] | None = None
    last_current_systemcalc: dict[str, Any] | None = None
    last_hypothetical_systemcalc: dict[str, Any] | None = None

    for event in _load_vebus_voltage_events(vebus_paths):
        timestamp = event["timestamp"]
        while current_frame is not None and current_frame.timestamp <= timestamp:
            cache.apply(current_frame)
            current_frame = next(frames, None)
        stateful = shadow.step(
            cache.snapshot(at=timestamp),
            timestamp=timestamp,
            vebus_voltage_v=event["value"],
        )
        current_systemcalc = simulate_systemcalc(
            stateful, scenario=CURRENT_NO_BMS_SCENARIO
        )
        hypothetical_systemcalc = simulate_systemcalc(
            stateful, scenario=HYPOTHETICAL_VIRTUAL_BMS_SCENARIO
        )
        samples += 1
        lifecycle = stateful["lifecycle"]["state"]
        lifecycle_counts[lifecycle] = lifecycle_counts.get(lifecycle, 0) + 1
        current_no_bms_lost_samples += current_systemcalc["bms_connection_lost_alarm"]
        hypothetical_selected_bms_lost_samples += hypothetical_systemcalc[
            "bms_connection_lost_alarm"
        ]
        voltage_alarm_samples += int(
            stateful["stateful_alarms"].get(
                "/Diagnostics/Alarms/VoltageDisagreement", 0
            )
            > 0
        )
        last_stateful = stateful
        last_current_systemcalc = current_systemcalc
        last_hypothetical_systemcalc = hypothetical_systemcalc

    while current_frame is not None:
        cache.apply(current_frame)
        current_frame = next(frames, None)

    alarm_events = [
        event for event in shadow.transitions if event["event"] == "alarm_transition"
    ]
    lifecycle_events = [
        event for event in shadow.transitions if event["event"] == "lifecycle_transition"
    ]
    return {
        "mode": "pc-only-stateful-venus-simulation",
        "action_taken": False,
        "registered_on_dbus": False,
        "transmitted_can": False,
        "remote_contacted": False,
        "writes_settings": False,
        "writes_vebus_mode": False,
        "candidate_timing_and_hysteresis_values": True,
        "can_paths": [str(path) for path in can_paths],
        "vebus_paths": [str(path) for path in vebus_paths],
        "samples": samples,
        "lifecycle_sample_counts": lifecycle_counts,
        "lifecycle_events": lifecycle_events,
        "alarm_events": alarm_events,
        "hypothetical_notification_count": len(alarm_events),
        "voltage_alarm_samples": voltage_alarm_samples,
        "current_no_bms_bms_lost_samples": current_no_bms_lost_samples,
        "hypothetical_selected_bms_lost_samples": hypothetical_selected_bms_lost_samples,
        "final_stateful_model": last_stateful,
        "final_current_no_bms_systemcalc": last_current_systemcalc,
        "final_hypothetical_selected_bms_systemcalc": last_hypothetical_systemcalc,
        "can_statistics": cache.snapshot()["statistics"],
    }


def _iter_can_frames(paths: list[Path]) -> Iterator[CanFrame]:
    # The SSH recorder can interleave chunks read from candump by a few hundred
    # milliseconds.  Preserve the files and reorder only in memory.  The
    # observed maximum inversion is 0.276 s, so a one-second window is ample
    # while keeping replay streaming and bounded.
    reorder_window_seconds = 1.0
    heap: list[tuple[float, int, CanFrame]] = []
    maximum_seen: float | None = None
    sequence = 0
    for path in paths:
        with path.open(encoding="ascii", errors="replace") as stream:
            for line in stream:
                try:
                    frame = parse_candump_line(line)
                except CandumpParseError:
                    continue
                sequence += 1
                maximum_seen = (
                    frame.timestamp
                    if maximum_seen is None
                    else max(maximum_seen, frame.timestamp)
                )
                if frame.timestamp < maximum_seen - reorder_window_seconds:
                    raise ValueError(
                        "CAN capture timestamp moved backwards by more than the "
                        "one-second replay reorder window"
                    )
                heapq.heappush(heap, (frame.timestamp, sequence, frame))
                cutoff = maximum_seen - reorder_window_seconds
                while heap and heap[0][0] <= cutoff:
                    yield heapq.heappop(heap)[2]
    while heap:
        yield heapq.heappop(heap)[2]


def _load_vebus_voltage_events(paths: list[Path]) -> list[dict[str, float]]:
    events: list[dict[str, float]] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("record_type") != "value" or record.get("path") != "/Dc/0/Voltage":
                    continue
                timestamp = record.get("timestamp")
                value = record.get("value")
                if isinstance(timestamp, (int, float)) and isinstance(value, (int, float)):
                    events.append({"timestamp": float(timestamp), "value": float(value)})
    events.sort(key=lambda event: event["timestamp"])
    return events


def _max_optional(current: float | None, candidate: float) -> float:
    return candidate if current is None else max(current, candidate)


def _event_rank(event: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(event["integration_alarm_level"]),
        abs(float(event["vebus_minus_deye_v"])),
        float(event["vebus_voltage_v"]),
    )
