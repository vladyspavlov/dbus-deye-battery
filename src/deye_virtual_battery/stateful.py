"""Stateful, PC-only shadow service and alarm lifecycle.

The classes in this module operate only on dictionaries and caller-supplied
timestamps.  They do not import a D-Bus or CAN library and cannot contact the
GX.  Timing values are deliberately provisional simulation candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dbus_model import build_mock_dbus_model
from .policy import PolicyConfig


@dataclass(frozen=True, slots=True)
class StatefulConfig:
    """Candidate timing and hysteresis values for offline simulation."""

    startup_ready_seconds: float = 2.0
    reconnect_ready_seconds: float = 3.0
    can_alive_timeout_seconds: float = 3.0
    # Victron's official MK2/MK3 protocol specifies that the Multi stops
    # charging/inverting after five minutes without BOL communication.  The
    # local countdown is deliberately conservative: it starts when this model
    # first invalidates the BMS paths, while installed systemcalc may take up
    # to its next three-second adjustment cycle to stop BOL writes.
    vebus_bol_timeout_seconds: float = 300.0
    # Lower CVL immediately when charge becomes blocked, but require a stable
    # higher target before raising it again.  This follows Victron's warning
    # that a CVL control loop should be slow and generally not update more than
    # once per roughly twenty seconds.
    cvl_raise_qualification_seconds: float = 20.0
    warning_raise_seconds: float = 2.0
    # Level-2 protections and >58.4 V VE.Bus observations are not debounced;
    # directional control is also always based on instantaneous policy.  Only
    # warning-level chatter is delayed.
    alarm_raise_seconds: float = 0.0
    alarm_downgrade_seconds: float = 5.0
    alarm_clear_seconds: float = 10.0
    voltage_warning_clear_v: float = 0.5
    voltage_alarm_clear_v: float = 4.0
    vebus_warning_clear_v: float = 57.2
    vebus_alarm_clear_v: float = 57.9


@dataclass(slots=True)
class _AlarmState:
    level: int = 0
    candidate: int = 0
    candidate_since: float | None = None


@dataclass(slots=True)
class DebouncedAlarmBank:
    """Debounce alarm raises and hold clears/downgrades for hysteresis."""

    config: StatefulConfig = field(default_factory=StatefulConfig)
    states: dict[str, _AlarmState] = field(default_factory=dict)

    def update(
        self, raw_levels: dict[str, int], *, timestamp: float
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        for path in sorted(set(self.states) | set(raw_levels)):
            state = self.states.setdefault(path, _AlarmState())
            target = _alarm_level(raw_levels.get(path, 0))
            if target == state.level:
                state.candidate = target
                state.candidate_since = None
                continue
            if target != state.candidate or state.candidate_since is None:
                state.candidate = target
                state.candidate_since = timestamp
            delay = self._transition_delay(state.level, target)
            if timestamp - state.candidate_since + 1e-9 < delay:
                continue
            previous = state.level
            state.level = target
            state.candidate_since = None
            events.append(
                {
                    "timestamp": timestamp,
                    "event": "alarm_transition",
                    "path": path,
                    "previous_level": previous,
                    "new_level": target,
                    "hypothetical_notification": True,
                    "action_taken": False,
                }
            )
        return ({path: state.level for path, state in sorted(self.states.items())}, events)

    def _transition_delay(self, current: int, target: int) -> float:
        if target > current:
            return (
                self.config.alarm_raise_seconds
                if target == 2
                else self.config.warning_raise_seconds
            )
        if target == 0:
            return self.config.alarm_clear_seconds
        return self.config.alarm_downgrade_seconds


@dataclass(slots=True)
class StatefulShadow:
    """Turn instantaneous shadow policies into a simulated service lifecycle."""

    policy_config: PolicyConfig = field(default_factory=PolicyConfig)
    stateful_config: StatefulConfig = field(default_factory=StatefulConfig)
    lifecycle_state: str = "starting"
    ready_since: float | None = None
    ever_online: bool = False
    last_step_timestamp: float | None = None
    bol_loss_since: float | None = None
    published_cvl_v: float | None = None
    cvl_raise_candidate_v: float | None = None
    cvl_raise_candidate_since: float | None = None
    alarm_bank: DebouncedAlarmBank = field(init=False)
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.alarm_bank = DebouncedAlarmBank(self.stateful_config)

    def step(
        self,
        snapshot: dict[str, Any],
        *,
        timestamp: float,
        vebus_voltage_v: float | None = None,
    ) -> dict[str, Any]:
        """Advance the offline state machine at ``timestamp``."""

        if self.last_step_timestamp is not None and timestamp < self.last_step_timestamp:
            raise ValueError("stateful simulation timestamps must be nondecreasing")
        self.last_step_timestamp = timestamp

        instantaneous = build_mock_dbus_model(
            snapshot,
            vebus_voltage_v=vebus_voltage_v,
            config=self.policy_config,
        )
        policy = instantaneous["policy"]
        last_frame = snapshot.get("last_timestamp")
        can_age = (
            max(0.0, timestamp - float(last_frame))
            if isinstance(last_frame, (int, float))
            else None
        )
        can_alive = (
            can_age is not None
            and can_age <= self.stateful_config.can_alive_timeout_seconds
        )
        ready_now = bool(policy["critical_data_ready"] and can_alive)
        lifecycle_events = self._advance_lifecycle(ready_now, can_alive, timestamp)
        bol_loss_active = self.ever_online and self.lifecycle_state != "online"
        if bol_loss_active:
            if self.bol_loss_since is None:
                self.bol_loss_since = timestamp
        else:
            self.bol_loss_since = None
        bol_timeout_remaining = (
            max(
                0.0,
                self.stateful_config.vebus_bol_timeout_seconds
                - (timestamp - self.bol_loss_since),
            )
            if self.bol_loss_since is not None
            else None
        )

        # BMS alarms and warnings are telemetry, not control inputs: they are
        # published for the operator and must never move a charge or discharge
        # limit.  Publish standard Deye alarm levels immediately; debounce only
        # local diagnostics and notification-oriented observations.
        raw_standard_alarms = dict(policy["standard_alarms"])
        raw_custom_alarms = dict(policy["custom_alarms"])
        raw_custom_alarms["/Diagnostics/Alarms/VoltageDisagreement"] = (
            self._voltage_alarm_target(policy)
        )
        if self.lifecycle_state == "starting":
            # A future service would not be published yet.  Suppress startup
            # alarm notifications until a complete data set has qualified.
            raw_standard_alarms = {
                path: None if level is None else 0
                for path, level in raw_standard_alarms.items()
            }
            raw_custom_alarms = {path: 0 for path in raw_custom_alarms}
        elif self.lifecycle_state in {"can_lost", "critical_data_stale", "reconnecting"}:
            raw_standard_alarms = {path: None for path in raw_standard_alarms}
            raw_custom_alarms["/Diagnostics/Alarms/CriticalDataStale"] = 2
        else:
            raw_custom_alarms["/Diagnostics/Alarms/CriticalDataStale"] = 0

        stateful_custom_alarms, alarm_events = self.alarm_bank.update(
            raw_custom_alarms, timestamp=timestamp
        )
        for event in alarm_events:
            event["context"] = {
                "raw_level": raw_custom_alarms.get(event["path"], 0),
                "lifecycle_state": self.lifecycle_state,
                "deye_pack_voltage_v": policy["voltage_observations"][
                    "deye_pack_voltage_v"
                ],
                "vebus_voltage_v": policy["voltage_observations"]["vebus_voltage_v"],
                "vebus_minus_deye_v": policy["voltage_observations"][
                    "vebus_minus_deye_v"
                ],
                "effective_limits": policy["effective_limits"],
                "allow_charge": policy["permissions"]["allow_charge"],
                "allow_discharge": policy["permissions"]["allow_discharge"],
            }
        events = lifecycle_events + alarm_events
        self.transitions.extend(events)

        hypothetical_registered = self.ever_online
        connected = self.lifecycle_state == "online"
        paths = dict(instantaneous["paths"])
        stateful_alarms = dict(raw_standard_alarms)
        stateful_alarms.update(stateful_custom_alarms)
        for path, level in stateful_alarms.items():
            paths[path] = level
        paths["/Connected"] = int(connected)
        paths["/Diagnostics/Lifecycle/State"] = self.lifecycle_state
        paths["/Diagnostics/Lifecycle/CanAlive"] = int(can_alive)
        paths["/Diagnostics/Lifecycle/CanAge"] = can_age
        paths["/Diagnostics/Lifecycle/EverOnline"] = int(self.ever_online)
        paths["/Diagnostics/Lifecycle/VebusBolLossWindowActive"] = int(
            bol_loss_active
        )
        paths["/Diagnostics/Lifecycle/VebusBolTimeoutSeconds"] = (
            self.stateful_config.vebus_bol_timeout_seconds
        )
        paths["/Diagnostics/Lifecycle/VebusBolTimeoutRemaining"] = (
            bol_timeout_remaining
        )
        paths["/Diagnostics/Lifecycle/VebusBolTimeoutExpected"] = int(
            bol_timeout_remaining == 0.0
        )
        self._apply_cvl_transition(paths, timestamp=timestamp, connected=connected)
        if not connected:
            self._invalidate_consumer_paths(paths)

        return {
            "mode": "pc-only-stateful-shadow",
            "action_taken": False,
            "registered_on_dbus": False,
            "would_register_mock_service": hypothetical_registered,
            "transmitted_can": False,
            "remote_contacted": False,
            "timestamp": timestamp,
            "lifecycle": {
                "state": self.lifecycle_state,
                "ready_now": ready_now,
                "can_alive": can_alive,
                "can_age_seconds": can_age,
                "connected": connected,
                "loss_reason": self._loss_reason(can_alive, policy),
                "candidate_timing_values": True,
            },
            "paths": paths,
            "instantaneous_policy": policy,
            "stateful_alarms": stateful_alarms,
            "events": events,
        }

    def _advance_lifecycle(
        self, ready: bool, can_alive: bool, timestamp: float
    ) -> list[dict[str, Any]]:
        previous = self.lifecycle_state
        if self.lifecycle_state == "starting":
            if ready:
                self.ready_since = timestamp if self.ready_since is None else self.ready_since
                if timestamp - self.ready_since >= self.stateful_config.startup_ready_seconds:
                    self.lifecycle_state = "online"
                    self.ever_online = True
                    self.ready_since = None
            else:
                self.ready_since = None
        elif self.lifecycle_state == "online":
            if not ready:
                self.lifecycle_state = "critical_data_stale" if can_alive else "can_lost"
                self.ready_since = None
        elif self.lifecycle_state in {"can_lost", "critical_data_stale"}:
            if ready:
                self.lifecycle_state = "reconnecting"
                self.ready_since = timestamp
            else:
                self.lifecycle_state = "critical_data_stale" if can_alive else "can_lost"
        elif self.lifecycle_state == "reconnecting":
            if not ready:
                self.lifecycle_state = "critical_data_stale" if can_alive else "can_lost"
                self.ready_since = None
            else:
                self.ready_since = timestamp if self.ready_since is None else self.ready_since
                if timestamp - self.ready_since >= self.stateful_config.reconnect_ready_seconds:
                    self.lifecycle_state = "online"
                    self.ready_since = None

        if previous == self.lifecycle_state:
            return []
        return [
            {
                "timestamp": timestamp,
                "event": "lifecycle_transition",
                "previous_state": previous,
                "new_state": self.lifecycle_state,
                "action_taken": False,
            }
        ]

    def _loss_reason(self, can_alive: bool, policy: dict[str, Any]) -> str | None:
        if self.lifecycle_state == "starting":
            return "waiting_for_complete_qualified_dataset"
        if self.lifecycle_state == "reconnecting":
            return "waiting_for_reconnect_qualification"
        if self.lifecycle_state == "can_lost":
            return "no_can_frames_within_timeout"
        if self.lifecycle_state == "critical_data_stale":
            missing = policy["missing_or_stale_control_fields"]
            return "critical_fields_stale:" + ",".join(missing)
        return None

    def _voltage_alarm_target(self, policy: dict[str, Any]) -> int:
        observations = policy["voltage_observations"]
        delta = observations["vebus_minus_deye_v"]
        vebus = observations["vebus_voltage_v"]
        if not isinstance(delta, (int, float)) or not isinstance(vebus, (int, float)):
            return 0
        absolute_delta = abs(float(delta))
        current = self.alarm_bank.states.get(
            "/Diagnostics/Alarms/VoltageDisagreement", _AlarmState()
        ).level
        raw_level = int(
            policy["custom_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"]
        )
        if raw_level > current:
            return raw_level
        if current == 2:
            if (
                absolute_delta >= self.stateful_config.voltage_alarm_clear_v
                or vebus >= self.stateful_config.vebus_alarm_clear_v
            ):
                return 2
        if current >= 1:
            if (
                absolute_delta >= self.stateful_config.voltage_warning_clear_v
                or vebus >= self.stateful_config.vebus_warning_clear_v
            ):
                return 1
        return raw_level

    def _apply_cvl_transition(
        self, paths: dict[str, Any], *, timestamp: float, connected: bool
    ) -> None:
        target = paths.get("/Info/MaxChargeVoltage") if connected else None
        if not isinstance(target, (int, float)):
            self.cvl_raise_candidate_v = None
            self.cvl_raise_candidate_since = None
        elif self.published_cvl_v is None or target < self.published_cvl_v:
            # A lower limit is safety-restrictive and must not be delayed.
            self.published_cvl_v = float(target)
            self.cvl_raise_candidate_v = None
            self.cvl_raise_candidate_since = None
        elif target > self.published_cvl_v:
            if self.cvl_raise_candidate_v != float(target):
                self.cvl_raise_candidate_v = float(target)
                self.cvl_raise_candidate_since = timestamp
            elif (
                self.cvl_raise_candidate_since is not None
                and timestamp - self.cvl_raise_candidate_since
                >= self.stateful_config.cvl_raise_qualification_seconds
            ):
                self.published_cvl_v = float(target)
                self.cvl_raise_candidate_v = None
                self.cvl_raise_candidate_since = None
        else:
            self.cvl_raise_candidate_v = None
            self.cvl_raise_candidate_since = None

        if connected:
            paths["/Info/MaxChargeVoltage"] = self.published_cvl_v
        paths["/Diagnostics/Cvl/Target"] = target
        paths["/Diagnostics/Cvl/Applied"] = self.published_cvl_v
        paths["/Diagnostics/Cvl/RaisePending"] = int(
            self.cvl_raise_candidate_since is not None
        )
        paths["/Diagnostics/Cvl/RaiseQualificationSeconds"] = (
            self.stateful_config.cvl_raise_qualification_seconds
        )

    @staticmethod
    def _invalidate_consumer_paths(paths: dict[str, Any]) -> None:
        prefixes = ("/Dc/", "/Info/", "/Io/")
        exact = {
            "/Soc",
            "/Soh",
            "/Capacity",
            "/ConsumedAmphours",
            "/System/MaxCellVoltage",
            "/System/MinCellVoltage",
            "/System/MaxCellTemperature",
            "/System/MinCellTemperature",
            "/System/NrOfModulesOnline",
            "/System/NrOfModulesBlockingCharge",
            "/System/NrOfModulesBlockingDischarge",
            "/System/NrOfModulesOffline",
        }
        for path in list(paths):
            if path.startswith(prefixes) or path in exact:
                paths[path] = None


def _alarm_level(value: Any) -> int:
    if not isinstance(value, (int, float)):
        return 0
    return 2 if value >= 2 else 1 if value >= 1 else 0
