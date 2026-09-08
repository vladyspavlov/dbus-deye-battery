"""PC-only control-policy model for the Deye virtual battery.

This module deliberately returns decisions as data.  It does not publish to
D-Bus, transmit CAN, or write VE.Bus state.  The provisional voltage values in
``PolicyConfig`` exist so captures can exercise a complete policy; they are not
approved production settings.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .decoder import VICTRON_ALARM_ACTIVE, VICTRON_ALARM_PATHS
from .profile import UNKNOWN, VICTRON_CAN


STANDARD_ALARM_PATHS = (
    "/Alarms/LowVoltage",
    "/Alarms/HighVoltage",
    "/Alarms/HighCellVoltage",
    "/Alarms/LowCellVoltage",
    "/Alarms/LowSoc",
    "/Alarms/HighChargeCurrent",
    "/Alarms/HighDischargeCurrent",
    "/Alarms/CellImbalance",
    "/Alarms/InternalFailure",
    "/Alarms/HighChargeTemperature",
    "/Alarms/LowChargeTemperature",
    "/Alarms/LowTemperature",
    "/Alarms/HighTemperature",
    "/Alarms/Contactor",
    "/Alarms/FuseBlown",
)

# Deye V3.3 does not define a low-SOC warning/alarm bit.  An invalid D-Bus
# value is the native-service equivalent of the Victron CAN protocol's
# "not supported" state; publishing zero would incorrectly claim support.
UNSUPPORTED_STANDARD_ALARM_PATHS = {"/Alarms/LowSoc"}


# The mapping is intentionally semantic.  Conditions for which Venus has no
# honest standard alarm remain available in diagnostics instead of being
# mislabeled.
CONDITION_ALARMS: dict[str, tuple[str, int]] = {
    "cell_over_voltage": ("/Alarms/HighCellVoltage", 2),
    "cell_under_voltage": ("/Alarms/LowCellVoltage", 2),
    "module_over_voltage": ("/Alarms/HighVoltage", 2),
    "module_under_voltage": ("/Alarms/LowVoltage", 2),
    "charge_over_current": ("/Alarms/HighChargeCurrent", 2),
    "discharge_over_current": ("/Alarms/HighDischargeCurrent", 2),
    "cell_over_temperature_charge": ("/Alarms/HighChargeTemperature", 2),
    "cell_under_temperature_charge": ("/Alarms/LowChargeTemperature", 2),
    "afe_over_current_discharge_1": ("/Alarms/HighDischargeCurrent", 2),
    "afe_over_current_discharge_2": ("/Alarms/HighDischargeCurrent", 2),
    "cell_voltage_difference_high": ("/Alarms/CellImbalance", 2),
    "mos_over_temperature": ("/Alarms/HighTemperature", 2),
    "cell_over_temperature_discharge": ("/Alarms/HighTemperature", 2),
    "cell_under_temperature_discharge": ("/Alarms/LowTemperature", 2),
    "heating_film_over_temperature": ("/Alarms/HighTemperature", 2),
    "afe_under_voltage": ("/Alarms/LowVoltage", 2),
    "afe_over_voltage": ("/Alarms/HighVoltage", 2),
    "afe_over_current_charge": ("/Alarms/HighChargeCurrent", 2),
    "afe_short_circuit_discharge": ("/Alarms/HighDischargeCurrent", 2),
    "afe_under_temperature": ("/Alarms/LowTemperature", 2),
    "afe_over_temperature": ("/Alarms/HighTemperature", 2),
    "afe_short_circuit_discharge_latched": ("/Alarms/HighDischargeCurrent", 2),
    "afe_over_current_discharge_latched": ("/Alarms/HighDischargeCurrent", 2),
    "cell_voltage_sampling_failure": ("/Alarms/InternalFailure", 2),
    "mosfet_short_circuit": ("/Alarms/InternalFailure", 2),
    "eeprom_error": ("/Alarms/InternalFailure", 2),
    "afe_communication_failure": ("/Alarms/InternalFailure", 2),
    "temperature_sampling_failure": ("/Alarms/InternalFailure", 2),
    "internal_communication_failure": ("/Alarms/InternalFailure", 2),
    "pcs_communication_failure": ("/Alarms/InternalFailure", 2),
    "master_address_duplicate": ("/Alarms/InternalFailure", 2),
    "cell_high_voltage_warning": ("/Alarms/HighCellVoltage", 1),
    "cell_low_voltage_warning": ("/Alarms/LowCellVoltage", 1),
    "module_high_voltage_warning": ("/Alarms/HighVoltage", 1),
    "module_low_voltage_warning": ("/Alarms/LowVoltage", 1),
    "charge_high_current_warning": ("/Alarms/HighChargeCurrent", 1),
    "discharge_high_current_warning": ("/Alarms/HighDischargeCurrent", 1),
    "cell_high_temperature_charge_warning": ("/Alarms/HighChargeTemperature", 1),
    "cell_low_temperature_charge_warning": ("/Alarms/LowChargeTemperature", 1),
    "cell_voltage_difference_warning": ("/Alarms/CellImbalance", 1),
    "mos_high_temperature_warning": ("/Alarms/HighTemperature", 1),
    "cell_high_temperature_discharge_warning": ("/Alarms/HighTemperature", 1),
    "cell_low_temperature_discharge_warning": ("/Alarms/LowTemperature", 1),
    "heating_film_high_temperature_warning": ("/Alarms/HighTemperature", 1),
    "connector_high_temperature": ("/Alarms/HighTemperature", 2),
    "terminal_high_temperature": ("/Alarms/HighTemperature", 2),
    "fuse_blown": ("/Alarms/FuseBlown", 2),
    "voltage_open_wire": ("/Alarms/InternalFailure", 2),
    "precharge_failed": ("/Alarms/Contactor", 2),
    "charge_reversed": ("/Alarms/InternalFailure", 2),
    "temperature_open_wire": ("/Alarms/InternalFailure", 2),
    "system_minor_fault": ("/Alarms/InternalFailure", 1),
    "system_major_fault": ("/Alarms/InternalFailure", 2),
}


CHARGE_STOP_CONDITIONS = {
    "cell_over_voltage",
    "module_over_voltage",
    "charge_over_current",
    "cell_over_temperature_charge",
    "cell_under_temperature_charge",
    "afe_over_voltage",
    "afe_over_current_charge",
    "charge_reversed",
}

DISCHARGE_STOP_CONDITIONS = {
    "cell_under_voltage",
    "module_under_voltage",
    "discharge_over_current",
    "afe_over_current_discharge_1",
    "afe_over_current_discharge_2",
    "cell_over_temperature_discharge",
    "cell_under_temperature_discharge",
    "afe_under_voltage",
    "afe_short_circuit_discharge",
    "afe_short_circuit_discharge_latched",
    "afe_over_current_discharge_latched",
}

BOTH_STOP_CONDITIONS = {
    "cell_voltage_sampling_failure",
    "mosfet_short_circuit",
    "eeprom_error",
    "afe_communication_failure",
    "temperature_sampling_failure",
    "internal_communication_failure",
    "pcs_communication_failure",
    "master_address_duplicate",
    "fuse_blown",
    "voltage_open_wire",
    "precharge_failed",
    "temperature_open_wire",
    "system_major_fault",
}


# V3.3 labels these as protection/system-error conditions, but the public
# document does not state which power direction the PCS expects an inverter to
# inhibit.  Keep them visible without inventing a shutdown action.
NO_DIRECTIONAL_STOP_PROTECTION_CONDITIONS = {
    "cell_voltage_difference_high",
    "cell_temperature_difference_high",
    "mos_over_temperature",
    "heating_film_over_temperature",
    "afe_under_temperature",
    "afe_over_temperature",
    "connector_high_temperature",
    "terminal_high_temperature",
    "charge_voltage_low",
}


# Conditions without an honest standard Venus alarm retain their Deye
# severity here.  In particular, a protection must never be silently
# downgraded to a warning merely because Venus has no exact standard path.
UNMAPPED_CONDITION_SEVERITIES = {
    "cell_temperature_difference_high": 2,
    "charge_voltage_low": 2,
    "cell_temperature_difference_warning": 1,
    "heating_mos_adhesion": 1,
    "heating_error": 1,
}


# Control fields both BMS-side profiles transmit.  0x110 stays available in
# the victronCAN profile, so pack conditions and MOS state remain required.
COMMON_REQUIRED_CONTROL_FIELDS = (
    "battery.voltage",
    "battery.current",
    "battery.soc",
    "limits.max_charge_voltage",
    "limits.max_charge_current",
    "limits.max_discharge_current",
    "pack.active_v33_conditions",
    "mos.charge_closed",
    "mos.discharge_closed",
)

# The Deye native profile also sends the 0x371 array limits and the 0x359
# system fault tables.  The victronCAN profile sends neither; requiring them
# there would drop the battery service about three seconds after the battery
# is switched, which is exactly the outage this adapter exists to avoid.
DEYE_NATIVE_REQUIRED_CONTROL_FIELDS = (
    "limits.array_max_charge_current",
    "limits.array_max_discharge_current",
    "alarms.active_v33_conditions",
)

REQUIRED_CONTROL_FIELDS = (
    COMMON_REQUIRED_CONTROL_FIELDS + DEYE_NATIVE_REQUIRED_CONTROL_FIELDS
)


def required_control_fields(
    profile: str, *, in_transition: bool = False
) -> tuple[str, ...]:
    """Return the control fields the active BMS-side profile must supply.

    An unresolved profile keeps the Deye requirement set, because the battery
    ships in that profile and a missing 0x356 sign convention already holds
    ``battery.current`` invalid until the profile is known.

    Immediately after a profile change the incoming profile's extra frames have
    not arrived yet, even though every frame that carries control and
    measurement data - 0x351, 0x355, 0x356 and 0x110 - has been continuous
    across both observed switches.  Requiring the extra frames during that
    settling window would drop the battery service for several seconds and
    start the VE.Bus operational-limit timeout for no protective benefit, so
    only the common set is required until the window expires.
    """

    if in_transition or profile == VICTRON_CAN:
        return COMMON_REQUIRED_CONTROL_FIELDS
    return REQUIRED_CONTROL_FIELDS


# A series count is only believable if the pack voltage really is that many
# cells of the measured size.  Anything outside this range, or that fails the
# consistency check, means the reading is not trustworthy and the fallback is
# used.
MINIMUM_CELL_COUNT = 4
MAXIMUM_CELL_COUNT = 32
# Slack on the min/max cell bracket, absorbing BMS rounding (cell voltages are
# reported in millivolts, pack voltage in tens of millivolts) and the small
# IR-drop difference between the pack sense point and the cell taps.
CELL_BRACKET_MARGIN_V = 0.05


def detect_cell_count(fields: dict[str, Any], fallback: int) -> tuple[int, bool]:
    """Work out the series cell count from the pack and cell voltages.

    The adapter is not tied to one model.  SE-F5, SE-F12 and SE-F16 differ in
    capacity and series count, and capacity already comes off the wire, so the
    only remaining model-specific number is how many cells are in series.
    Dividing the pack voltage by the mean cell voltage gives it directly.

    Returns ``(count, detected)``.  ``detected`` is False when the inputs are
    missing or do not agree, in which case the caller's fallback is used and
    the adapter says so rather than pretending it measured something.

    The count is only accepted if the per-cell voltage it implies actually
    lies between the reported minimum and maximum cell.  That has to be true
    of the real count -- a mean cannot fall outside its own extremes -- and it
    is what makes the answer trustworthy: a partially stale snapshot pairing
    one frame's pack voltage with another's cell voltages lands outside the
    bracket, and so does every neighbouring count.

    An earlier version compared the count against a fixed 0.25 V residual from
    the mean of the two extremes.  That mean is a biased estimator whenever the
    cells are unevenly spread, and on the reference 16s pack -- 54.5 V with
    cells at 3.352 and 3.524 -- the bias is 0.51 V, so the correct count was
    rejected and every pack fell back to the assumed 16.
    """
    pack_voltage = _number(fields, "battery.voltage")
    maximum = _maximum_effective(fields, ("cells.max_voltage_200", "cells.max_voltage_361"))
    minimum = _minimum_effective(fields, ("cells.min_voltage_200", "cells.min_voltage_361"))
    if pack_voltage is None or maximum is None or minimum is None:
        return fallback, False
    if minimum <= 0.5 or maximum < minimum:
        return fallback, False
    mean_cell = (maximum + minimum) / 2.0
    count = int(round(pack_voltage / mean_cell))
    if not MINIMUM_CELL_COUNT <= count <= MAXIMUM_CELL_COUNT:
        return fallback, False
    implied_cell = pack_voltage / count
    if not (
        minimum - CELL_BRACKET_MARGIN_V
        <= implied_cell
        <= maximum + CELL_BRACKET_MARGIN_V
    ):
        return fallback, False
    return count, True


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Provisional values used only by the local shadow model."""

    # Pack voltage thresholds are per cell, multiplied by the detected series
    # count, so one configuration covers every pack in the family instead of
    # hard-coding one model.  For a 16s pack these reproduce the previous fixed
    # values exactly: 3.60 -> 57.6, 3.65 -> 58.4, 3.575 -> 57.2, 3.45 -> 55.2.
    normal_max_cell_v: float = 3.60
    pack_overvoltage_cell_v: float = 3.65
    provisional_charge_ceiling_cell_v: float = 3.575
    blocked_charge_cell_v: float = 3.45
    # Used only when the series count cannot be measured from the wire.
    fallback_cell_count: int = 16
    cell_count: int = 16
    cell_count_detected: bool = False
    high_cell_protection_v: float = 3.65
    voltage_delta_warning_v: float = 1.0
    voltage_delta_alarm_v: float = 5.0
    # A healthy managed-battery integration keeps the charge current limit
    # well above a trickle during normal operation.  A quarter of the pack
    # capacity in Ah is the threshold this project reports against.  The
    # shortfall is only ever reported: the adapter must never invent a charge
    # current the battery did not request.
    victron_minimum_ccl_fraction: float = 0.25
    # Deriving CVL from the measured pack voltage minus an offset creates a
    # feedback loop that can walk the target voltage away from the intended
    # value, so it is deliberately not done here.  Use a fixed, conservative
    # value while the battery blocks charge.  55.2 V is 3.45 V/cell for a
    # 16-series pack and remains a commissioning value pending bench
    # validation -- see the calibration note in README.md.
    product_id: int = 0xFFFF
    device_instance: int = 513

    @property
    def normal_max_voltage_v(self) -> float:
        return round(self.normal_max_cell_v * self.cell_count, 3)

    @property
    def pack_overvoltage_protection_v(self) -> float:
        return round(self.pack_overvoltage_cell_v * self.cell_count, 3)

    @property
    def provisional_charge_ceiling_v(self) -> float:
        return round(self.provisional_charge_ceiling_cell_v * self.cell_count, 3)

    @property
    def blocked_charge_cvl_v(self) -> float:
        """Fixed conservative CVL used while the battery blocks charge.

        Deliberately not derived from measured pack voltage minus an offset:
        that creates a feedback loop that can walk the target away from the
        intended value. 3.45 V/cell remains a commissioning value pending bench
        validation -- see the calibration note in README.md.
        """
        return round(self.blocked_charge_cell_v * self.cell_count, 3)

    def for_pack(self, fields: dict[str, Any]) -> "PolicyConfig":
        """Return this configuration bound to the pack actually on the wire."""
        count, detected = detect_cell_count(fields, self.fallback_cell_count)
        if count == self.cell_count and detected == self.cell_count_detected:
            return self
        return replace(self, cell_count=count, cell_count_detected=detected)


def evaluate_policy(
    snapshot: dict[str, Any],
    *,
    vebus_voltage_v: float | None = None,
    config: PolicyConfig | None = None,
) -> dict[str, Any]:
    """Evaluate a decoded snapshot without performing any action."""

    config = config or PolicyConfig()
    fields = snapshot.get("fields", {})
    # Bind the pack thresholds to this battery's series count before any
    # voltage comparison uses them.
    config = config.for_pack(fields)
    profile_state = snapshot.get("protocol_profile") or {}
    profile = profile_state.get("profile", UNKNOWN)
    in_transition = bool(profile_state.get("in_transition"))
    required = required_control_fields(profile, in_transition=in_transition)
    missing = [name for name in required if _effective(fields, name) is None]
    critical_ready = not missing

    pack_voltage = _number(fields, "battery.voltage")
    raw_cvl = _number(fields, "limits.max_charge_voltage")
    raw_ccl = _number(fields, "limits.max_charge_current")
    raw_dcl = _number(fields, "limits.max_discharge_current")
    array_ccl = _number(fields, "limits.array_max_charge_current")
    array_dcl = _number(fields, "limits.array_max_discharge_current")
    charge_mos = _effective(fields, "mos.charge_closed")
    discharge_mos = _effective(fields, "mos.discharge_closed")

    conditions = set(
        set(_string_list(fields, "pack.active_v33_conditions"))
        | set(_string_list(fields, "alarms.active_v33_conditions"))
    )
    fault_level = _effective(fields, "system.fault_level")
    if fault_level == "minor":
        conditions.add("system_minor_fault")
    elif fault_level == "major":
        conditions.add("system_major_fault")
    conditions = sorted(conditions)
    standard_alarms: dict[str, int | None] = {
        path: None if path in UNSUPPORTED_STANDARD_ALARM_PATHS else 0
        for path in STANDARD_ALARM_PATHS
    }
    unmapped_conditions: list[str] = []
    for condition in conditions:
        mapping = CONDITION_ALARMS.get(condition)
        if mapping is None:
            unmapped_conditions.append(condition)
            continue
        path, severity = mapping
        previous = standard_alarms[path]
        standard_alarms[path] = max(previous or 0, severity)

    # Victron's own 0x35A frame, present only in the victronCAN profile, can
    # raise an alarm level but never lower one.  The live battery sends eight
    # zero bytes, which the specification defines as "not supported" for every
    # field, so the Deye 0x110 condition tables remain the real alarm source.
    victron_alarm_states = {
        name: _effective(fields, f"victron_alarms.{name}")
        for name in VICTRON_ALARM_PATHS
    }
    victron_active = sorted(
        name
        for name, state in victron_alarm_states.items()
        if state == VICTRON_ALARM_ACTIVE
    )
    for name in victron_active:
        path, severity = VICTRON_ALARM_PATHS[name]
        standard_alarms[path] = max(standard_alarms.get(path) or 0, severity)

    # Victron's controlling specification is explicit: warnings and alarms are
    # telemetry only, and 0x351 is the sole control message.  Do not turn an
    # alarm bit into a synthetic CCL/DCL veto.  Deye's fresh current limits and
    # actual MOS state remain authoritative.  Independent measured-voltage
    # safety checks below may still inhibit charge, but never discharge.
    charge_stop_conditions: list[str] = []
    discharge_stop_conditions: list[str] = []
    max_cell_voltage = _maximum_effective(
        fields, ("cells.max_voltage_200", "cells.max_voltage_361")
    )
    if pack_voltage is not None:
        if pack_voltage > config.pack_overvoltage_protection_v:
            standard_alarms["/Alarms/HighVoltage"] = 2
            charge_stop_conditions.append("pack_voltage_above_protection_threshold")
        elif pack_voltage > config.normal_max_voltage_v:
            standard_alarms["/Alarms/HighVoltage"] = max(
                standard_alarms["/Alarms/HighVoltage"] or 0, 1
            )
            charge_stop_conditions.append("pack_voltage_above_normal_maximum")
    if max_cell_voltage is not None and max_cell_voltage > config.high_cell_protection_v:
        standard_alarms["/Alarms/HighCellVoltage"] = 2
        charge_stop_conditions.append("cell_voltage_above_protection_threshold")

    # A sustained 0x356/0x150 direction disagreement means the profile-derived
    # sign may be inverted.  Inhibit charge, never discharge: stopping charge
    # cannot interrupt AC output, while a synthetic discharge stop can.
    sign_conflict = bool(
        (snapshot.get("health") or {}).get("current_sign_convention_conflict")
    )
    if sign_conflict:
        charge_stop_conditions.append("current_sign_convention_conflict")

    voltage_delta = None
    voltage_mismatch = False
    vebus_above_normal = False
    integration_alarm_level = 0
    if pack_voltage is not None and isinstance(vebus_voltage_v, (int, float)):
        voltage_delta = round(float(vebus_voltage_v) - pack_voltage, 3)
        voltage_mismatch = abs(voltage_delta) > config.voltage_delta_warning_v
        vebus_above_normal = float(vebus_voltage_v) > config.normal_max_voltage_v
        if voltage_mismatch or vebus_above_normal:
            integration_alarm_level = 1
        if (
            abs(voltage_delta) > config.voltage_delta_alarm_v
            or float(vebus_voltage_v) > config.pack_overvoltage_protection_v
        ):
            integration_alarm_level = 2
        if voltage_mismatch:
            charge_stop_conditions.append("vebus_battery_voltage_disagreement")
        if vebus_above_normal:
            charge_stop_conditions.append("vebus_voltage_above_deye_normal_maximum")

    charge_stop_conditions = sorted(set(charge_stop_conditions))
    discharge_stop_conditions = sorted(set(discharge_stop_conditions))
    charge_reasons: list[str] = list(charge_stop_conditions)
    discharge_reasons: list[str] = list(discharge_stop_conditions)

    effective_ccl: float | None = None
    effective_dcl: float | None = None
    effective_cvl: float | None = None
    # 0x250 carries the pack's own maximum allowable currents in both profiles.
    # It is the only remaining second opinion on charge current once the
    # victronCAN profile stops sending the 0x371 array limits, so it is applied
    # restrictively to charge only.  It must never reduce the discharge limit:
    # a stale low value there could stop an islanded Multi.
    pack_allowable_ccl = _number(fields, "pack.maximum_allowable_charge_current")
    # 0x250 is quantized to whole amps while 0x351 and 0x371 carry 0.1 A, so a
    # reported 55 A can mean anything up to 56 A.  Applying its ceiling keeps
    # the cross-check protective against a genuinely lower pack limit without
    # shaving a normal limit down by the quantization step.
    pack_allowable_ccl_ceiling = (
        pack_allowable_ccl + 1.0 if pack_allowable_ccl is not None else None
    )
    if critical_ready:
        assert raw_ccl is not None and raw_dcl is not None
        assert raw_cvl is not None and pack_voltage is not None

        charge_sources = [raw_ccl]
        if array_ccl is not None:
            charge_sources.append(array_ccl)
        if pack_allowable_ccl_ceiling is not None:
            charge_sources.append(pack_allowable_ccl_ceiling)
        effective_ccl = min(charge_sources)
        if effective_ccl <= 0:
            charge_reasons.append("ccl_frames_request_zero_charge")
        if charge_mos is not True:
            charge_reasons.append("charge_mos_open")
        if charge_reasons:
            effective_ccl = 0.0

        discharge_sources = [raw_dcl]
        if array_dcl is not None:
            discharge_sources.append(array_dcl)
        effective_dcl = min(discharge_sources)
        if effective_dcl <= 0:
            discharge_reasons.append("dcl_frames_request_zero_discharge")
        if discharge_mos is not True:
            discharge_reasons.append("discharge_mos_open")
        if discharge_reasons:
            effective_dcl = 0.0

        effective_cvl = min(raw_cvl, config.provisional_charge_ceiling_v)
        if effective_ccl <= 0:
            effective_cvl = min(effective_cvl, config.blocked_charge_cvl_v)

    charge_reasons = sorted(set(charge_reasons))
    discharge_reasons = sorted(set(discharge_reasons))
    unmapped_alarm_level = max(
        (UNMAPPED_CONDITION_SEVERITIES.get(name, 1) for name in unmapped_conditions),
        default=0,
    )
    active_set = set(conditions)
    uncoordinated_protections = set(
        active_set & NO_DIRECTIONAL_STOP_PROTECTION_CONDITIONS
    )
    if (
        active_set & (CHARGE_STOP_CONDITIONS | BOTH_STOP_CONDITIONS)
        and isinstance(raw_ccl, (int, float))
        and raw_ccl > 0
        and charge_mos is True
    ):
        uncoordinated_protections.update(
            active_set & (CHARGE_STOP_CONDITIONS | BOTH_STOP_CONDITIONS)
        )
    if (
        active_set & (DISCHARGE_STOP_CONDITIONS | BOTH_STOP_CONDITIONS)
        and isinstance(raw_dcl, (int, float))
        and raw_dcl > 0
        and discharge_mos is True
    ):
        uncoordinated_protections.update(
            active_set & (DISCHARGE_STOP_CONDITIONS | BOTH_STOP_CONDITIONS)
        )
    uncoordinated_protections = sorted(uncoordinated_protections)
    custom_alarms = {
        "/Diagnostics/Alarms/VoltageDisagreement": integration_alarm_level,
        "/Diagnostics/Alarms/CriticalDataStale": 0 if critical_ready else 2,
        "/Diagnostics/Alarms/UnmappedDeyeCondition": unmapped_alarm_level,
        "/Diagnostics/Alarms/ProtectionWithoutDirectionalLimit": (
            2 if uncoordinated_protections else 0
        ),
        "/Diagnostics/Alarms/CurrentSignConflict": 2 if sign_conflict else 0,
        "/Diagnostics/Alarms/UnresolvedBmsProfile": 2 if profile == UNKNOWN else 0,
    }

    installed_capacity = _number(fields, "battery.installed_capacity")
    victron_minimum_ccl = (
        round(installed_capacity * config.victron_minimum_ccl_fraction, 1)
        if installed_capacity is not None
        else None
    )
    ccl_below_victron_recommendation = (
        victron_minimum_ccl is not None
        and isinstance(raw_ccl, (int, float))
        and raw_ccl < victron_minimum_ccl
    )

    return {
        "mode": "pc-only-shadow-policy",
        "action_taken": False,
        "publishes_to_dbus": False,
        "transmits_can": False,
        "writes_vebus_mode": False,
        "production_control_allowed": False,
        "production_blockers": [
            "fixed active/blocked CVL candidates require bench and vendor validation",
            "Deye routinely requests CCL=0 at full charge, contrary to Victron CVL-based-control guidance; the adapter must not invent charge current while the charge MOS is open",
            "active Deye alarm-bit transitions have not all been observed on this battery",
            "stateful loss/reconnect behavior is simulated but requires exact-version Venus validation",
            "a Victron-assigned ProductId is required before claiming official support",
            "the victronCAN battery profile has only been observed for 29 seconds at rest; its alarm, limit and fault behaviour under load is unvalidated",
        ],
        "bms_protocol_profile": profile,
        "bms_profile_in_transition": in_transition,
        "required_control_fields": list(required),
        "critical_data_ready": critical_ready,
        "missing_or_stale_control_fields": missing,
        "service_publishable": critical_ready,
        "raw_limits": {
            "cvl_v": raw_cvl,
            "ccl_a": raw_ccl,
            "array_ccl_a": array_ccl,
            "dcl_a": raw_dcl,
            "array_dcl_a": array_dcl,
            "pack_allowable_ccl_a": pack_allowable_ccl,
            "pack_allowable_ccl_ceiling_a": pack_allowable_ccl_ceiling,
        },
        "effective_limits": {
            "cvl_v": effective_cvl,
            "ccl_a": effective_ccl,
            "dcl_a": effective_dcl,
            "battery_low_voltage_v": _number(fields, "limits.battery_low_voltage"),
            "provisional": True,
        },
        "permissions": {
            "allow_charge": effective_ccl is not None and effective_ccl > 0,
            "allow_discharge": effective_dcl is not None and effective_dcl > 0,
            "charge_inhibit_reasons": charge_reasons,
            "discharge_inhibit_reasons": discharge_reasons,
            "would_request_discharge_stop": effective_dcl == 0.0,
            "may_stop_inverter_when_islanded": effective_dcl == 0.0,
            "would_directly_switch_inverter_off": False,
        },
        "standard_alarms": standard_alarms,
        "custom_alarms": custom_alarms,
        "active_deye_conditions": conditions,
        "unmapped_deye_conditions": unmapped_conditions,
        "protections_without_directional_limit": uncoordinated_protections,
        "voltage_observations": {
            "deye_pack_voltage_v": pack_voltage,
            "vebus_voltage_v": vebus_voltage_v,
            "vebus_minus_deye_v": voltage_delta,
            "mismatch": voltage_mismatch,
            "vebus_above_deye_normal_maximum": vebus_above_normal,
            "detection_does_not_assert_deye_high_voltage_alarm": (
                integration_alarm_level > 0
                and standard_alarms["/Alarms/HighVoltage"] == 0
            ),
        },
        "diagnostics": {
            "charge_mos_closed": charge_mos,
            "discharge_mos_closed": discharge_mos,
            "maximum_cell_voltage_v": max_cell_voltage,
            "system_fault_level": fault_level,
            "bms_protocol_profile": profile,
            "victron_minimum_recommended_ccl_a": victron_minimum_ccl,
            "ccl_below_victron_recommendation": ccl_below_victron_recommendation,
            "victron_system_status_raw": _effective(
                fields, "victron_alarms.system_status_raw"
            ),
            "victron_alarm_fields_active": victron_active,
            "victron_alarm_fields_reported": sorted(
                name
                for name, state in victron_alarm_states.items()
                if isinstance(state, str) and state != "not_supported"
            ),
            "current_sign_convention_conflict": sign_conflict,
            "array_limits_available": array_ccl is not None and array_dcl is not None,
            "pack_allowable_ccl_applied": pack_allowable_ccl is not None,
            "provisional_charge_ceiling_v": config.provisional_charge_ceiling_v,
            "blocked_charge_cvl_v": config.blocked_charge_cvl_v,
            "alarm_flags_affect_directional_limits": False,
        },
    }


def _effective(fields: dict[str, Any], name: str) -> Any:
    state = fields.get(name)
    if not isinstance(state, dict):
        return None
    return state.get("effective_value")


def _number(fields: dict[str, Any], name: str) -> float | None:
    value = _effective(fields, name)
    return float(value) if isinstance(value, (int, float)) else None


def _string_list(fields: dict[str, Any], name: str) -> list[str]:
    value = _effective(fields, name)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _maximum_effective(fields: dict[str, Any], names: tuple[str, ...]) -> float | None:
    values = [_number(fields, name) for name in names]
    present = [value for value in values if value is not None]
    return max(present) if present else None



def _minimum_effective(fields: dict[str, Any], names: tuple[str, ...]) -> float | None:
    values = [_number(fields, name) for name in names]
    present = [value for value in values if value is not None]
    return min(present) if present else None
