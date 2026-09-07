"""Per-field freshness cache for the PC-only virtual battery shadow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .candump import CanFrame
from .decoder import CRITICAL, OPTIONAL, SESSION, TRANSPORT, DecodedField, DecodeError, DeyeDecoder
from .profile import (
    CURRENT_SIGN_FACTORS,
    DEYE_NATIVE,
    UNKNOWN,
    VICTRON_CAN,
    ProtocolProfileDetector,
)


#: Fields whose source frames the named profile does not transmit.  They go
#: stale by design after a profile change and must not be reported as a fault.
DEYE_ONLY_FIELDS = frozenset(
    {
        "alarms.raw_alarm_word",
        "alarms.raw_warning_word",
        "alarms.active_v33_conditions",
        "alarms.any_v33_condition_active",
        "alarms.table_1_raw",
        "alarms.table_2_raw",
        "alarms.table_3_raw",
        "alarms.table_4_raw",
        "alarms.table_5_raw",
        "alarms.table_6_raw",
        "alarms.table_7_raw",
        "requests.raw_35c_flags",
        "requests.charge_enable_v33",
        "requests.discharge_enable_v33",
        "requests.force_charge_1",
        "requests.force_charge_2",
        "requests.full_charge_request",
        "requests.request_heat",
        "requests.reserved_35c_bits",
        "modules.normal",
        "modules.charge_disabled",
        "modules.discharge_disabled",
        "modules.communication_disconnected",
        "modules.parallel_connected",
        "modules.reserved_364",
        "limits.array_max_charge_current",
        "limits.array_max_discharge_current",
        "limits.reserved_371",
    }
)

VICTRON_ONLY_FIELDS = frozenset(
    {
        "victron_alarms.frame_35a_seen",
        "victron_alarms.supported_fields",
        "victron_alarms.active_fields",
        "victron_alarms.any_supported_field",
        "victron_alarms.system_status_raw",
        "victron_alarms.raw_bytes",
    }
)

INACTIVE_PROFILE_FIELDS = {
    DEYE_NATIVE: VICTRON_ONLY_FIELDS,
    VICTRON_CAN: DEYE_ONLY_FIELDS,
}


TIMEOUTS: dict[str, float | None] = {
    # Victron declares loss of 0x351 after three seconds.  Deye's live control
    # frames arrive about once per second, so matching that deadline leaves
    # room for ordinary jitter without retaining limits longer than Victron's
    # controlling protocol permits.
    CRITICAL: 3.0,
    TRANSPORT: 3.0,
    OPTIONAL: 180.0,
    SESSION: None,
    "configuration": None,
}


@dataclass(slots=True)
class FieldState:
    name: str
    cached_value: Any
    unit: str | None
    category: str
    source_can_id: int | None
    last_valid_timestamp: float | None
    confidence: str
    note: str | None
    updates: int = 1
    rejected_updates: int = 0
    last_rejected_timestamp: float | None = None
    last_rejected_value: Any = None

    def update(self, decoded: DecodedField, timestamp: float) -> None:
        if decoded.valid:
            self.cached_value = decoded.value
            self.unit = decoded.unit
            self.category = decoded.category
            self.source_can_id = decoded.source_can_id
            self.last_valid_timestamp = timestamp
            self.confidence = decoded.confidence
            self.note = decoded.note
            self.updates += 1
        else:
            self.rejected_updates += 1
            self.last_rejected_timestamp = timestamp
            self.last_rejected_value = decoded.value

    def snapshot(self, at: float) -> dict[str, Any]:
        timeout = TIMEOUTS[self.category]
        if self.last_valid_timestamp is None:
            age = None
            if self.category == "configuration":
                freshness = "configured"
                usable = True
            else:
                freshness = "invalid"
                usable = False
        else:
            age = max(0.0, at - self.last_valid_timestamp)
            freshness = "fresh" if timeout is None or age <= timeout else "stale"
            usable = freshness != "stale"
        return {
            "cached_value": self.cached_value,
            "effective_value": self.cached_value if usable else None,
            "unit": self.unit,
            "category": self.category,
            "source_can_id": f"0x{self.source_can_id:03X}" if self.source_can_id is not None else "configuration",
            "last_valid_timestamp": self.last_valid_timestamp,
            "last_valid_utc": _timestamp_text(self.last_valid_timestamp),
            "age_seconds": round(age, 6) if age is not None else None,
            "freshness": freshness,
            "usable": usable,
            "confidence": self.confidence,
            "note": self.note,
            "updates": self.updates,
            "rejected_updates": self.rejected_updates,
            "last_rejected_timestamp": self.last_rejected_timestamp,
            "last_rejected_value": self.last_rejected_value,
        }


def _timestamp_text(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


#: Explanation published with the derived Victron-convention current.
PROFILE_CURRENT_NOTES = {
    DEYE_NATIVE: (
        "Deye sign inverted for Victron convention: confirmed during the "
        "2026-08-30 Inverter-only discharge test"
    ),
    VICTRON_CAN: (
        "victronCAN profile already sends Victron's convention: positive "
        "means charge (2026-09-01 profile switch, 24 of 24 paired samples)"
    ),
    UNKNOWN: (
        "BMS-side protocol profile unresolved: the 0x356 sign convention is "
        "unknown, so no Victron-convention current is published"
    ),
}


#: Minimum absolute current at which the 0x356/0x150 sign cross-check is
#: meaningful.  Both frames quantize to 0.1 A and disagree by a few tenths at
#: rest, so only clearly non-zero, same-direction samples are compared.
SIGN_CROSSCHECK_MINIMUM_A = 1.0

#: Consecutive contradicting samples required before the published current is
#: withdrawn.  At the observed one-second cadence this is a few seconds.
SIGN_CROSSCHECK_DISAGREEMENTS = 5


@dataclass(slots=True)
class VirtualBatteryCache:
    installed_capacity_ah: float = 230.0
    profile_detector: ProtocolProfileDetector = field(
        default_factory=ProtocolProfileDetector
    )
    decoder: DeyeDecoder | None = None
    fields: dict[str, FieldState] = field(default_factory=dict)
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    total_frames: int = 0
    decoded_frames: int = 0
    unknown_frames: int = 0
    decode_errors: int = 0
    ids: dict[int, int] = field(default_factory=dict)
    stream_last_timestamp: dict[str, float] = field(default_factory=dict)
    stream_max_gap: dict[str, float] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    anomaly_events: list[dict[str, Any]] = field(default_factory=list)
    protocol_events: list[dict[str, Any]] = field(default_factory=list)
    positive_ccl_started: dict[str, tuple[float, float]] = field(default_factory=dict)
    synced_profile_transitions: int = 0
    sign_crosscheck_agreements: int = 0
    sign_crosscheck_disagreements: int = 0
    sign_crosscheck_streak: int = 0
    sign_convention_conflict: bool = False

    def __post_init__(self) -> None:
        if self.decoder is None:
            self.decoder = DeyeDecoder(profile_resolver=self.profile_detector.resolve)
        self.fields["battery.installed_capacity"] = FieldState(
            name="battery.installed_capacity",
            cached_value=self.installed_capacity_ah,
            unit="Ah",
            category="configuration",
            source_can_id=None,
            last_valid_timestamp=None,
            confidence="configured",
            note="pack model configuration; not inferred from a missing frame",
        )

    def apply(self, frame: CanFrame) -> list[DecodedField]:
        self.total_frames += 1
        self.ids[frame.can_id] = self.ids.get(frame.can_id, 0) + 1
        stream_key = f"0x{frame.can_id:03X}:{frame.direction or '?'}"
        previous_stream_timestamp = self.stream_last_timestamp.get(stream_key)
        if previous_stream_timestamp is not None and frame.timestamp >= previous_stream_timestamp:
            gap = frame.timestamp - previous_stream_timestamp
            self.stream_max_gap[stream_key] = max(
                gap, self.stream_max_gap.get(stream_key, 0.0)
            )
        self.stream_last_timestamp[stream_key] = frame.timestamp
        if self.first_timestamp is None:
            self.first_timestamp = frame.timestamp
        self.last_timestamp = frame.timestamp
        if frame.direction != "T":
            # Classify the BMS-side protocol profile before decoding, so the
            # 0x356 sign convention for this frame follows the frames that
            # arrived with it rather than the previous cycle.
            self.profile_detector.observe(
                frame.can_id, frame.payload, frame.timestamp
            )
            self._sync_profile_events()
        assert self.decoder is not None
        try:
            updates = self.decoder.decode(frame)
        except DecodeError as error:
            self.decode_errors += 1
            self.errors.append(
                {
                    "timestamp": frame.timestamp,
                    "can_id": f"0x{frame.can_id:03X}",
                    "error": str(error),
                }
            )
            self.errors = self.errors[-100:]
            return []
        if not updates:
            self.unknown_frames += 1
            return []
        self.decoded_frames += 1
        for decoded in updates:
            existing = self.fields.get(decoded.name)
            if decoded.name == "battery.soc" and decoded.valid:
                self._observe_soc_transition(existing, decoded, frame.timestamp)
            if decoded.name == "battery.current_raw_deye" and decoded.valid:
                self._observe_current_sign(decoded, frame.timestamp)
            if decoded.valid:
                self._observe_protocol_transition(existing, decoded, frame.timestamp)
            if existing is None:
                if decoded.valid:
                    self.fields[decoded.name] = FieldState(
                        name=decoded.name,
                        cached_value=decoded.value,
                        unit=decoded.unit,
                        category=decoded.category,
                        source_can_id=decoded.source_can_id,
                        last_valid_timestamp=frame.timestamp,
                        confidence=decoded.confidence,
                        note=decoded.note,
                    )
                else:
                    self.fields[decoded.name] = FieldState(
                        name=decoded.name,
                        cached_value=None,
                        unit=decoded.unit,
                        category=decoded.category,
                        source_can_id=decoded.source_can_id,
                        last_valid_timestamp=None,
                        confidence=decoded.confidence,
                        note=decoded.note,
                        updates=0,
                        rejected_updates=1,
                        last_rejected_timestamp=frame.timestamp,
                        last_rejected_value=decoded.value,
                    )
            else:
                existing.update(decoded, frame.timestamp)
        return updates

    def _observe_protocol_transition(
        self, existing: FieldState | None, decoded: DecodedField, timestamp: float
    ) -> None:
        if existing is None or existing.last_valid_timestamp is None:
            return
        previous = existing.cached_value
        current = decoded.value
        if previous == current:
            return

        tracked = {
            "limits.max_charge_current",
            "limits.array_max_charge_current",
            "mos.charge_closed",
            "mos.discharge_closed",
            "system.operation_mode_code",
            "system.fault_level_code",
            "system.substate_raw",
        }
        if decoded.name in tracked:
            self._add_protocol_event(
                timestamp,
                decoded.name,
                previous,
                current,
                decoded.source_can_id,
            )

        if decoded.name not in {
            "limits.max_charge_current",
            "limits.array_max_charge_current",
        }:
            return
        if not isinstance(previous, (int, float)) or not isinstance(current, (int, float)):
            return

        if previous <= 0 < current:
            self.positive_ccl_started[decoded.name] = (timestamp, float(current))
            charge_mos = self._fresh_cached_value("mos.charge_closed", timestamp)
            if charge_mos is False:
                self._add_anomaly(
                    timestamp,
                    "positive_ccl_with_charge_mos_open",
                    {
                        "field": decoded.name,
                        "charge_current_limit_a": current,
                        "charge_mos_closed": False,
                    },
                )
        elif previous > 0 >= current:
            started = self.positive_ccl_started.pop(decoded.name, None)
            if started is None:
                return
            started_at, peak = started
            duration = timestamp - started_at
            raw_current = self._fresh_cached_value("battery.current_raw_deye", timestamp)
            charge_mos = self._fresh_cached_value("mos.charge_closed", timestamp)
            if (
                duration <= 10
                and isinstance(raw_current, (int, float))
                and abs(raw_current) <= 0.1
            ):
                self._add_anomaly(
                    timestamp,
                    "ccl_pulse_without_measured_charge",
                    {
                        "field": decoded.name,
                        "peak_charge_current_limit_a": peak,
                        "duration_seconds": round(duration, 6),
                        "raw_deye_current_a": raw_current,
                        "charge_mos_closed": charge_mos,
                    },
                )

    def _sync_profile_events(self) -> None:
        """Copy any new detector transitions into the protocol event log.

        The detector can also change profile when its evidence expires during a
        ``resolve`` call, so draining it here keeps one record of every
        transition regardless of which call observed it.
        """

        transitions = self.profile_detector.transitions
        while self.synced_profile_transitions < len(transitions):
            transition = transitions[self.synced_profile_transitions]
            self.synced_profile_transitions += 1
            self.protocol_events.append(
                {
                    "timestamp": transition["timestamp"],
                    "utc": _timestamp_text(transition["timestamp"]),
                    "field": "protocol.bms_profile",
                    "previous": transition["previous_profile"],
                    "current": transition["new_profile"],
                    "source_can_id": "profile-detector",
                    "action_taken": False,
                }
            )
            self.protocol_events = self.protocol_events[-5000:]
            # A profile change re-bases the 0x356 sign convention, so the
            # running cross-check must not carry contradictions across the
            # boundary.
            self.sign_crosscheck_streak = 0
            self.sign_convention_conflict = False

    def _observe_current_sign(self, decoded: DecodedField, timestamp: float) -> None:
        """Cross-check the profile-selected 0x356 sign against 0x150.

        ``0x150`` keeps the Deye current convention in both profiles, so once
        both values are converted to Victron's convention they must agree.  A
        sustained contradiction means the profile classification is wrong;
        withdrawing the published current is safer than publishing a value
        whose direction may be inverted.
        """

        if self.profile_detector.in_transition(timestamp):
            return
        reference = self.fields.get("diagnostics.current_victron_150")
        if (
            reference is None
            or reference.last_valid_timestamp is None
            or timestamp - reference.last_valid_timestamp > TIMEOUTS[CRITICAL]
        ):
            return
        factor = CURRENT_SIGN_FACTORS.get(self.profile_detector.resolve(timestamp))
        if factor is None or not isinstance(decoded.value, (int, float)):
            return
        published = decoded.value * factor
        observed = reference.cached_value
        if not isinstance(published, (int, float)) or not isinstance(observed, (int, float)):
            return
        if (
            abs(published) < SIGN_CROSSCHECK_MINIMUM_A
            or abs(observed) < SIGN_CROSSCHECK_MINIMUM_A
        ):
            return
        if published * observed > 0:
            self.sign_crosscheck_agreements += 1
            self.sign_crosscheck_streak = 0
            self.sign_convention_conflict = False
            return
        self.sign_crosscheck_disagreements += 1
        self.sign_crosscheck_streak += 1
        if (
            self.sign_crosscheck_streak >= SIGN_CROSSCHECK_DISAGREEMENTS
            and not self.sign_convention_conflict
        ):
            self.sign_convention_conflict = True
            self._add_anomaly(
                timestamp,
                "current_sign_convention_disagreement",
                {
                    "profile": self.profile_detector.resolve(timestamp),
                    "published_current_a": published,
                    "frame_150_victron_current_a": observed,
                    "consecutive_disagreements": self.sign_crosscheck_streak,
                },
            )

    def _fresh_cached_value(self, name: str, timestamp: float) -> Any:
        state = self.fields.get(name)
        if state is None or state.last_valid_timestamp is None:
            return None
        timeout = TIMEOUTS[state.category]
        if timeout is not None and timestamp - state.last_valid_timestamp > timeout:
            return None
        return state.cached_value

    def _add_protocol_event(
        self,
        timestamp: float,
        field_name: str,
        previous: Any,
        current: Any,
        source_can_id: int,
    ) -> None:
        self.protocol_events.append(
            {
                "timestamp": timestamp,
                "utc": _timestamp_text(timestamp),
                "field": field_name,
                "previous": previous,
                "current": current,
                "source_can_id": f"0x{source_can_id:03X}",
                "action_taken": False,
            }
        )
        self.protocol_events = self.protocol_events[-5000:]

    def _observe_soc_transition(
        self, existing: FieldState | None, decoded: DecodedField, timestamp: float
    ) -> None:
        if existing is None or existing.last_valid_timestamp is None:
            return
        previous = existing.cached_value
        elapsed = timestamp - existing.last_valid_timestamp
        if not isinstance(previous, (int, float)) or elapsed < 0:
            return
        voltage_state = self.fields.get("battery.voltage")
        voltage = None
        if (
            voltage_state is not None
            and voltage_state.last_valid_timestamp is not None
            and timestamp - voltage_state.last_valid_timestamp <= TIMEOUTS[CRITICAL]
        ):
            voltage = voltage_state.cached_value
        if decoded.value == 0 and previous != 0 and isinstance(voltage, (int, float)) and voltage > 50:
            self._add_anomaly(
                timestamp,
                "soc_zero_at_high_voltage",
                {
                    "previous_soc_percent": previous,
                    "reported_soc_percent": decoded.value,
                    "pack_voltage_v": voltage,
                    "elapsed_seconds": elapsed,
                },
            )
        if elapsed <= 10 and decoded.value <= previous - 20:
            self._add_anomaly(
                timestamp,
                "implausibly_fast_soc_drop",
                {
                    "previous_soc_percent": previous,
                    "reported_soc_percent": decoded.value,
                    "pack_voltage_v": voltage,
                    "elapsed_seconds": elapsed,
                },
            )

    def _add_anomaly(self, timestamp: float, event: str, details: dict[str, Any]) -> None:
        self.anomaly_events.append(
            {
                "timestamp": timestamp,
                "utc": _timestamp_text(timestamp),
                "event": event,
                "details": details,
                "action_taken": False,
            }
        )
        self.anomaly_events = self.anomaly_events[-1000:]

    def apply_all(self, frames: Iterable[CanFrame]) -> None:
        for frame in frames:
            self.apply(frame)

    def snapshot(self, at: float | None = None) -> dict[str, Any]:
        reference = at if at is not None else self.last_timestamp
        if reference is None:
            reference = datetime.now(tz=timezone.utc).timestamp()
        self.profile_detector.resolve(reference)
        self._sync_profile_events()
        field_snapshots = {
            name: state.snapshot(reference) for name, state in sorted(self.fields.items())
        }
        field_snapshots.update(self._derived_fields(field_snapshots, reference))
        field_snapshots = dict(sorted(field_snapshots.items()))
        return {
            "mode": "pc-only-shadow",
            "publishes_to_dbus": False,
            "transmits_can": False,
            "protocol_profile": self.profile_detector.snapshot(reference),
            "reference_timestamp": reference,
            "reference_utc": _timestamp_text(reference),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "statistics": {
                "total_frames": self.total_frames,
                "decoded_frames": self.decoded_frames,
                "unknown_or_wrong_direction_frames": self.unknown_frames,
                "decode_errors": self.decode_errors,
                "can_id_counts": {
                    f"0x{can_id:03X}": count for can_id, count in sorted(self.ids.items())
                },
                "maximum_interframe_gap_seconds": {
                    stream: round(gap, 6)
                    for stream, gap in sorted(self.stream_max_gap.items())
                },
            },
            "health": self._health(field_snapshots, reference),
            "fields": field_snapshots,
            "recent_errors": self.errors,
            "protocol_events": self.protocol_events,
            "anomaly_events": self.anomaly_events,
        }

    def _derived_fields(
        self, fields: dict[str, dict[str, Any]], reference: float
    ) -> dict[str, dict[str, Any]]:
        """Derive the Victron-convention current and power from cached data.

        The 0x356 current sign depends on which BMS-side protocol profile the
        battery is transmitting.  Deriving it here rather than in the frame
        decoder keeps one source of truth, makes the published value follow a
        profile change on the next snapshot instead of the next 0x356, and
        removes any dependence on frame order within a one-second cycle.
        """

        raw = fields.get("battery.current_raw_deye")
        voltage = fields.get("battery.voltage")
        if raw is None:
            return {}
        profile = self.profile_detector.resolve(reference)
        factor = CURRENT_SIGN_FACTORS.get(profile)
        note = PROFILE_CURRENT_NOTES.get(profile, PROFILE_CURRENT_NOTES[UNKNOWN])
        wire_value = raw["effective_value"]
        usable = (
            raw["usable"]
            and factor is not None
            and isinstance(wire_value, (int, float))
        )
        if self.sign_convention_conflict:
            # The value stays published so the battery service stays online;
            # withdrawing it would drop /Connected and start the VE.Bus
            # operational-limit timeout over a telemetry disagreement.  The
            # policy raises a diagnostic alarm and inhibits charge instead.
            note = (
                "0x356 and 0x150 disagree about the current direction; the "
                "profile-selected sign may be wrong and charge is inhibited"
            )
        current_value = (
            round(float(wire_value) * factor, 1)
            if usable and factor is not None and isinstance(wire_value, (int, float))
            else None
        )
        current = dict(raw)
        current.update(
            {
                "cached_value": current_value,
                "effective_value": current_value,
                "usable": usable,
                "freshness": raw["freshness"] if usable else "invalid",
                "confidence": "high",
                "note": note,
            }
        )
        voltage_value = voltage["effective_value"] if voltage else None
        power_value = (
            round(float(voltage_value) * current_value, 3)
            if current_value is not None and isinstance(voltage_value, (int, float))
            else None
        )
        if self.sign_convention_conflict:
            current["confidence"] = "conflicting"
        power = dict(current)
        power.update(
            {
                "cached_value": power_value,
                "effective_value": power_value,
                "unit": "W",
                "usable": usable and power_value is not None,
            }
        )
        return {"battery.current": current, "battery.power_candidate": power}

    def _health(
        self, fields: dict[str, dict[str, Any]], reference: float
    ) -> dict[str, Any]:
        profile = self.profile_detector.resolve(reference)
        inactive = INACTIVE_PROFILE_FIELDS.get(profile, frozenset())
        critical_stale = [
            name
            for name, value in fields.items()
            if value["category"] == CRITICAL and not value["usable"]
        ]
        # Frames the other profile owns are absent by design, not by fault.
        critical_stale_active_profile = [
            name
            for name in critical_stale
            if name not in inactive
            and not (profile == DEYE_NATIVE and name.startswith("victron_alarms."))
        ]
        rejected = [name for name, value in fields.items() if value["rejected_updates"]]
        voltage = _effective(fields, "battery.voltage")
        voltage_150 = _effective(fields, "diagnostics.voltage_150")
        current = _effective(fields, "battery.current")
        current_150 = _effective(fields, "diagnostics.current_victron_150")
        voltage_delta = (
            round(abs(voltage - voltage_150), 3)
            if isinstance(voltage, (int, float)) and isinstance(voltage_150, (int, float))
            else None
        )
        current_delta = (
            round(abs(current - current_150), 3)
            if isinstance(current, (int, float)) and isinstance(current_150, (int, float))
            else None
        )
        soc_state = fields.get("battery.soc")
        if soc_state is None:
            soc_observation = "missing"
        elif not soc_state["usable"]:
            soc_observation = soc_state["freshness"]
        elif soc_state["effective_value"] == 0:
            soc_observation = "reported_zero"
        else:
            soc_observation = "valid"
        # Use the profile-corrected Victron-convention current: the raw 0x356
        # wire sign means the opposite thing in the two battery profiles.
        measured_current = _effective(fields, "battery.current")
        charge_mos_closed = _effective(fields, "mos.charge_closed")
        discharge_mos_closed = _effective(fields, "mos.discharge_closed")
        ccl = _effective(fields, "limits.max_charge_current")
        array_ccl = _effective(fields, "limits.array_max_charge_current")
        request_charge_enable = _effective(fields, "requests.charge_enable_v33")
        module_charge_disabled = _effective(fields, "modules.charge_disabled")
        if isinstance(measured_current, (int, float)) and measured_current > 0.1:
            charge_path_state = "charging"
        elif isinstance(measured_current, (int, float)) and measured_current < -0.1:
            charge_path_state = "discharging"
        elif (
            charge_mos_closed is False
            or (isinstance(ccl, (int, float)) and ccl <= 0)
            or (isinstance(array_ccl, (int, float)) and array_ccl <= 0)
        ):
            charge_path_state = "blocked_or_isolated"
        elif charge_mos_closed is True and isinstance(ccl, (int, float)) and ccl > 0 and (
            array_ccl is None or (isinstance(array_ccl, (int, float)) and array_ccl > 0)
        ):
            # The victronCAN profile does not send the 0x371 array limits, so a
            # missing array value must not block the permitted-idle summary.
            charge_path_state = "permitted_idle"
        else:
            charge_path_state = "unknown"
        charge_signal_disagreement = any(
            (
                request_charge_enable is False and charge_mos_closed is True,
                request_charge_enable is False and isinstance(ccl, (int, float)) and ccl > 0,
                isinstance(module_charge_disabled, (int, float))
                and module_charge_disabled > 0
                and charge_mos_closed is True,
            )
        )
        return {
            "bms_protocol_profile": profile,
            "current_sign_factor": CURRENT_SIGN_FACTORS.get(profile),
            "current_sign_crosscheck_agreements": self.sign_crosscheck_agreements,
            "current_sign_crosscheck_disagreements": self.sign_crosscheck_disagreements,
            "current_sign_convention_conflict": self.sign_convention_conflict,
            "profile_transition_recent": self.profile_detector.in_transition(reference),
            "critical_stale_fields": critical_stale,
            "critical_stale_fields_for_active_profile": critical_stale_active_profile,
            "fields_with_rejected_updates": rejected,
            "raw_alarm_active": bool(_effective(fields, "alarms.raw_alarm_word") or 0),
            "raw_warning_active": bool(_effective(fields, "alarms.raw_warning_word") or 0),
            "high_cvl_zero_ccl": bool(_effective(fields, "diagnostics.high_cvl_zero_ccl") or False),
            # v0.71 selects Pylontech only on the ASCII "PN" marker in 0x359
            # bytes 5-6.  The victronCAN profile stops sending 0x359 entirely,
            # so the stock decoder would still fall through to LG 0xB004.
            "stock_victron_identity_hazard": profile == VICTRON_CAN or bool(
                _effective(fields, "diagnostics.stock_victron_would_select_lg") or False
            ),
            "wire_identity_claims_pylontech": bool(
                _effective(fields, "identity.victron_profile_marker") or False
            ),
            "victron_alarm_frame_present": (
                _effective(fields, "victron_alarms.frame_35a_seen") is True
            ),
            "victron_alarm_fields_supported": bool(
                _effective(fields, "victron_alarms.any_supported_field") or False
            ),
            "voltage_356_vs_150_delta_v": voltage_delta,
            "voltage_crosscheck_over_1v": voltage_delta is not None and voltage_delta > 1.0,
            "current_356_vs_150_delta_a": current_delta,
            "charge_path_state": charge_path_state,
            "charge_mos_closed": charge_mos_closed,
            "discharge_mos_closed": discharge_mos_closed,
            "system_ccl_allows_charge": isinstance(ccl, (int, float)) and ccl > 0,
            "array_ccl_allows_charge": isinstance(array_ccl, (int, float)) and array_ccl > 0,
            "v33_charge_enable_summary": request_charge_enable,
            "charge_disabled_module_count": module_charge_disabled,
            "charge_signal_disagreement": charge_signal_disagreement,
            "v33_pack_condition_active": bool(_effective(fields, "pack.any_v33_condition_active") or False),
            "v33_system_condition_active": bool(_effective(fields, "alarms.any_v33_condition_active") or False),
            "system_operation_mode": _effective(fields, "system.operation_mode"),
            "system_fault_level": _effective(fields, "system.fault_level"),
            "system_substate_raw": _effective(fields, "system.substate_raw"),
            "soc_observation": soc_observation,
            "fresh_305_request": _effective(fields, "transport.request_305_seen") is True,
            "fresh_307_identity": _effective(fields, "transport.identity_307_seen") is True,
            "anomaly_event_count": len(self.anomaly_events),
            "protocol_event_count": len(self.protocol_events),
            "ccl_pulse_without_measured_charge_count": sum(
                event["event"] == "ccl_pulse_without_measured_charge"
                for event in self.anomaly_events
            ),
            "control_ready": False,
            "control_ready_reason": "Stateful policy exists only as a PC shadow and is not production-approved",
        }


def _effective(fields: dict[str, dict[str, Any]], name: str) -> Any:
    state = fields.get(name)
    return state["effective_value"] if state else None
