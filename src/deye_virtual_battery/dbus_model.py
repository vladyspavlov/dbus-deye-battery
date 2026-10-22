"""Build a D-Bus-shaped dictionary without opening or publishing to D-Bus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .decoder import MAX_PACKS, pack_field_name
from .packs import pack_numbers
from .version import VERSION
from .policy import PolicyConfig, evaluate_policy


def build_mock_dbus_model(
    snapshot: dict[str, Any],
    *,
    vebus_voltage_v: float | None = None,
    config: PolicyConfig | None = None,
) -> dict[str, Any]:
    """Return the paths a future Venus service would expose.

    The result is plain Python data and is safe to generate on any PC.  A
    production publisher will be a separate component and is intentionally not
    included yet.
    """

    config = config or PolicyConfig()
    policy = evaluate_policy(snapshot, vebus_voltage_v=vebus_voltage_v, config=config)
    fields = snapshot.get("fields", {})
    ready = policy["service_publishable"]
    limits = policy["effective_limits"]
    permissions = policy["permissions"]

    voltage = _effective(fields, "battery.voltage") if ready else None
    current = _effective(fields, "battery.current") if ready else None
    power = _effective(fields, "battery.power_candidate") if ready else None
    installed_capacity = _number(fields, "battery.installed_capacity")
    soc = _number(fields, "battery.soc") if ready else None
    modules_total = _number(fields, "modules.parallel_connected")
    modules_offline = _number(fields, "modules.communication_disconnected")
    online_capacity = installed_capacity if ready else None
    if (
        online_capacity is not None
        and modules_total is not None
        and modules_total > 0
        and modules_offline is not None
    ):
        online_fraction = max(0.0, modules_total - modules_offline) / modules_total
        online_capacity = round(online_capacity * online_fraction, 1)
    consumed_amphours = (
        round(online_capacity * (100.0 - soc) / 100.0, 1)
        if online_capacity is not None and soc is not None
        else None
    )
    # 0x35E already carries the bank's nominal capacity -- two 100 Ah packs
    # report 200 Ah -- so capacity is never summed again here.  What the GUI
    # needs from this driver is how many packs make up that bank.
    pack_count = _effective(fields, "bank.pack_count")
    batteries = pack_count if isinstance(pack_count, int) and pack_count > 0 else 1
    balancing = _effective(fields, "bank.balancing")
    paths: dict[str, Any] = {
        "/Mgmt/ProcessName": "deye-virtual-battery-shadow",
        "/Mgmt/ProcessVersion": f"{VERSION}-shadow",
        "/Mgmt/Connection": "recorded Deye PCS CAN (PC-only mock)",
        "/ProductId": config.product_id,
        "/ProductName": "Deye LV battery",
        "/CustomName": "Deye LV battery (shadow)",
        "/Manufacturer": "Deye",
        "/Serial": serial_number(fields),
        "/DeviceInstance": config.device_instance,
        "/Connected": 1 if ready else 0,
        "/Dc/0/Voltage": voltage,
        "/Dc/0/Current": current,
        "/Dc/0/Power": power,
        "/Dc/0/Temperature": _effective(fields, "battery.temperature") if ready else None,
        "/Soc": soc,
        "/Soh": _effective(fields, "battery.soh") if ready else None,
        "/InstalledCapacity": installed_capacity,
        # Victron defines /Capacity as usable online capacity, not remaining
        # charge.  SOC-derived consumed Ah is published separately.
        "/Capacity": online_capacity,
        "/ConsumedAmphours": consumed_amphours,
        "/Info/MaxChargeVoltage": limits["cvl_v"] if ready else None,
        "/Info/MaxChargeCurrent": limits["ccl_a"] if ready else None,
        "/Info/MaxDischargeCurrent": limits["dcl_a"] if ready else None,
        "/Info/BatteryLowVoltage": limits["battery_low_voltage_v"] if ready else None,
        "/Io/AllowToCharge": int(bool(permissions["allow_charge"])) if ready else None,
        "/Io/AllowToDischarge": int(bool(permissions["allow_discharge"])) if ready else None,
        "/System/NrOfCellsPerBattery": config.for_pack(fields).cell_count,
        # gui-v2 shows a battery's "System" page (batteries, parallel, series,
        # cells per battery) whenever /System/NrOfBatteries is valid.
        "/System/NrOfBatteries": batteries,
        "/System/BatteriesParallel": batteries,
        "/System/BatteriesSeries": 1,
        "/Balancing": int(balancing) if isinstance(balancing, bool) and ready else None,
        "/System/NrOfModulesOnline": (
            _effective(fields, "modules.normal") if ready else None
        ),
        "/System/NrOfModulesBlockingCharge": (
            _effective(fields, "modules.charge_disabled") if ready else None
        ),
        "/System/NrOfModulesBlockingDischarge": (
            _effective(fields, "modules.discharge_disabled") if ready else None
        ),
        "/System/NrOfModulesOffline": (
            _effective(fields, "modules.communication_disconnected") if ready else None
        ),
        "/System/MaxCellVoltage": _maximum_effective(
            fields, ("cells.max_voltage_200", "cells.max_voltage_361")
        ),
        "/System/MinCellVoltage": _minimum_effective(
            fields, ("cells.min_voltage_200", "cells.min_voltage_361")
        ),
        "/System/MaxCellTemperature": _maximum_effective(
            fields, ("cells.max_temperature_200", "cells.max_temperature_361")
        ),
        "/System/MinCellTemperature": _minimum_effective(
            fields, ("cells.min_temperature_200", "cells.min_temperature_361")
        ),
        # The Deye extrema frames contain voltages and temperatures but no cell
        # identifiers.  Explicit invalid values prevent an invented cell 0.
        "/System/MaxVoltageCellId": None,
        "/System/MinVoltageCellId": None,
        "/System/MaxTemperatureCellId": None,
        "/System/MinTemperatureCellId": None,
        "/History/ChargedEnergy": (
            _bank_or_pack(fields, "bank.charged_energy", "history.charged_energy")
            if ready
            else None
        ),
        "/History/DischargedEnergy": (
            _bank_or_pack(fields, "bank.discharged_energy", "history.discharged_energy")
            if ready
            else None
        ),
        "/History/ChargeCycles": (
            _bank_or_pack(fields, "bank.cycle_count", "battery.cycle_count")
            if ready
            else None
        ),
        "/Diagnostics/Policy/ProductionControlAllowed": 0,
        "/Diagnostics/Policy/WritesVebusMode": 0,
        "/Diagnostics/Policy/ProvisionalLimits": 1,
        "/Diagnostics/Deye/RawCvl": policy["raw_limits"]["cvl_v"],
        "/Diagnostics/Deye/RawCcl": policy["raw_limits"]["ccl_a"],
        "/Diagnostics/Deye/RawDcl": policy["raw_limits"]["dcl_a"],
        # 0x351 frames whose limits were refused, such as the all-zero frame
        # some parallel banks interleave with the real one.  The published
        # limits keep the last accepted frame's values meanwhile.
        "/Diagnostics/Deye/RejectedLimitFrames": (
            snapshot.get("statistics", {}).get("rejected_limit_frames", 0)
        ),
        "/Diagnostics/VebusDcVoltage": vebus_voltage_v,
        "/Diagnostics/VoltageDelta": policy["voltage_observations"]["vebus_minus_deye_v"],
        "/Diagnostics/VoltageMismatch": int(policy["voltage_observations"]["mismatch"]),
        "/Diagnostics/Capacity/Derived": int(
            modules_total is not None and modules_offline is not None
        ),
        "/Diagnostics/Capacity/Source": (
            "Deye nominal capacity adjusted by online module count"
            if modules_total is not None and modules_offline is not None
            else "Deye nominal capacity"
        ),
        "/Diagnostics/Cells/ExtremaIdsAvailable": 0,
        "/Diagnostics/Profile/BmsProtocol": policy["bms_protocol_profile"],
        "/Diagnostics/Profile/CurrentSignFactor": _profile_value(
            snapshot, "current_sign_factor"
        ),
        "/Diagnostics/Profile/InTransition": int(
            bool(_profile_value(snapshot, "in_transition"))
        ),
        "/Diagnostics/Profile/DeyeEvidenceAge": _profile_value(
            snapshot, "deye_evidence_age_seconds"
        ),
        "/Diagnostics/Profile/VictronEvidenceAge": _profile_value(
            snapshot, "victron_evidence_age_seconds"
        ),
        # The wire identity is reported, never adopted: the adapter publishes
        # its own neutral Product ID whatever name the battery sends in 0x35E.
        "/Diagnostics/Profile/WireIdentityClaimsPylontech": int(
            bool(_effective(fields, "identity.victron_profile_marker"))
        ),
        "/Diagnostics/Profile/VictronAlarmFrameSupportedFields": len(
            _effective(fields, "victron_alarms.supported_fields") or ()
        ),
        "/Diagnostics/Deye/ArrayLimitsAvailable": int(
            bool(policy["diagnostics"]["array_limits_available"])
        ),
        "/Diagnostics/Deye/PackAllowableCcl": policy["raw_limits"][
            "pack_allowable_ccl_a"
        ],
        # Reported against the quarter-of-capacity CCL threshold described in
        # policy.PolicyConfig.  The battery frequently requests far less; the
        # shortfall is surfaced as a diagnostic rather than corrected, because
        # inventing charge current the battery did not ask for would be
        # unsafe.
        "/Diagnostics/Victron/MinimumRecommendedCcl": policy["diagnostics"][
            "victron_minimum_recommended_ccl_a"
        ],
        "/Diagnostics/Victron/CclBelowRecommendation": int(
            bool(policy["diagnostics"]["ccl_below_victron_recommendation"])
        ),
        "/Diagnostics/Victron/SystemStatusRaw": policy["diagnostics"][
            "victron_system_status_raw"
        ],
    }
    paths["/Diagnostics/Bank/PackCount"] = pack_count
    paths["/Diagnostics/Bank/PacksOnline"] = len(_effective(fields, "bank.packs_online") or ())
    paths["/Diagnostics/Bank/Complete"] = _flag(_effective(fields, "bank.complete"))
    paths["/Diagnostics/Bank/ChargeMosClosed"] = _flag(
        _effective(fields, "bank.charge_mos_closed")
    )
    paths.update(pack_paths(fields, ready=ready))
    paths.update(policy["standard_alarms"])
    paths.update(policy["custom_alarms"])
    return {
        "mode": "pc-only-mock-dbus",
        "service_name": "com.victronenergy.battery.deye_lv",
        "registered_on_dbus": False,
        "action_taken": False,
        "paths": paths,
        "policy": policy,
    }


#: Per-pack paths, registered for every possible pack because a Venus service
#: registers its paths once.  Unused slots stay invalid.
PACK_PATH_FIELDS: tuple[tuple[str, str], ...] = (
    ("/Battery/{n}/Voltage", "diagnostics.voltage_150"),
    ("/Battery/{n}/Current", "diagnostics.current_victron_150"),
    ("/Battery/{n}/Soc", "diagnostics.soc_150"),
    ("/Battery/{n}/Temperature", "cells.max_temperature_200"),
    ("/Diagnostics/Pack/{n}/AllowableChargeCurrent", "pack.maximum_allowable_charge_current"),
    ("/Diagnostics/Pack/{n}/AllowableDischargeCurrent", "pack.maximum_allowable_discharge_current"),
    ("/Diagnostics/Pack/{n}/MaxCellVoltage", "cells.max_voltage_200"),
    ("/Diagnostics/Pack/{n}/MinCellVoltage", "cells.min_voltage_200"),
)


@dataclass(frozen=True, slots=True)
class _PackSlot:
    """Every path and field name of one pack slot, formatted once."""

    values: tuple[tuple[str, str], ...]
    serial: str
    online: str
    charge_mos: str
    discharge_mos: str
    prefix: str
    raw_flags: str
    charge_closed: str
    discharge_closed: str
    unused: dict[str, None]


def _pack_slot(pack: int) -> _PackSlot:
    values = tuple(
        (template.format(n=pack), pack_field_name(pack, name))
        for template, name in PACK_PATH_FIELDS
    )
    serial = f"/Battery/{pack}/Serial"
    online = f"/Diagnostics/Pack/{pack}/Online"
    charge_mos = f"/Diagnostics/Pack/{pack}/ChargeMosClosed"
    discharge_mos = f"/Diagnostics/Pack/{pack}/DischargeMosClosed"
    return _PackSlot(
        values=values,
        serial=serial,
        online=online,
        charge_mos=charge_mos,
        discharge_mos=discharge_mos,
        prefix=pack_field_name(pack, ""),
        raw_flags=pack_field_name(pack, "mos.raw_flags"),
        charge_closed=pack_field_name(pack, "mos.charge_closed"),
        discharge_closed=pack_field_name(pack, "mos.discharge_closed"),
        unused=dict.fromkeys(
            [path for path, _ in values] + [serial, online, charge_mos, discharge_mos]
        ),
    )


_PACK_SLOTS = {pack: _pack_slot(pack) for pack in range(1, MAX_PACKS + 1)}


def pack_paths(fields: dict[str, Any], *, ready: bool = True) -> dict[str, Any]:
    """Publish each pack of a parallel bank on its own.

    gui-v2's "Individual battery info" page reads ``/Battery/<n>/...`` for the
    pack selected through the writable ``/Battery/Request/Id``, which the
    publisher registers.  Values come from that pack's own frames
    (``0x150 + n - 1`` and so on); the Deye protocol carries no per-cell data,
    only each pack's extremes, which are published as diagnostics.
    """

    paths: dict[str, Any] = {}
    seen = set(_effective(fields, "bank.packs_seen") or ())
    # bank.packs_seen is this same scan of the snapshot's field names; only a
    # snapshot without the bank aggregates needs it done again.
    present = seen if "bank.packs_seen" in fields else set(pack_numbers(fields))
    for pack, slot in _PACK_SLOTS.items():
        if pack not in seen and pack not in present:
            # Nothing was ever decoded for this slot, so every path is
            # invalid.  Most slots are like this, and this runs four times a
            # second.
            paths.update(slot.unused)
            continue
        # A pack is online while its 0x110-family frame is fresh.  Its other
        # frames stay cached for minutes, so an offline pack publishes
        # nothing rather than its last readings.
        online = ready and _effective(fields, slot.raw_flags) is not None

        def value(name: str) -> Any:
            return _effective(fields, name) if online else None

        for path, name in slot.values:
            paths[path] = value(name)
        paths[slot.serial] = serial_number(fields, prefix=slot.prefix) if online else None
        # Invalid for a slot no pack has used; 0 for a pack that went silent.
        paths[slot.online] = int(online) if ready and pack in seen else None
        paths[slot.charge_mos] = _flag(value(slot.charge_closed))
        paths[slot.discharge_mos] = _flag(value(slot.discharge_closed))
    return paths


def _flag(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _bank_or_pack(fields: dict[str, Any], bank: str, pack: str) -> Any:
    """Prefer the bank aggregate; fall back to pack 1 when none is derived."""

    if bank in fields:
        return _effective(fields, bank)
    return _effective(fields, pack)


def _profile_value(snapshot: dict[str, Any], key: str) -> Any:
    profile = snapshot.get("protocol_profile")
    return profile.get(key) if isinstance(profile, dict) else None


def serial_number(fields: dict[str, Any], *, prefix: str = "") -> str | None:
    """Join the pack serial, which arrives split across two CAN frames.

    ``0x600`` carries the first eight characters and ``0x650`` the second
    eight; concatenated they are the serial the vendor app displays.  Both
    halves must have decoded as printable ASCII before anything is published:
    half a serial is worse than none, because it looks like a whole one.

    Returned as ``None`` -- Venus's invalid value -- until both are present.
    """
    first = _effective(fields, f"{prefix}identity.serial_first_half")
    second = _effective(fields, f"{prefix}identity.serial_second_half")
    if not isinstance(first, str) or not isinstance(second, str):
        return None
    # Vendors pad short serials; NULs cannot appear here because the decoder
    # only accepts printable ASCII, but trailing spaces do.
    serial = (first + second).strip()
    return serial or None


def _effective(fields: dict[str, Any], name: str) -> Any:
    state = fields.get(name)
    return state.get("effective_value") if isinstance(state, dict) else None


def _number(fields: dict[str, Any], name: str) -> float | None:
    value = _effective(fields, name)
    return float(value) if isinstance(value, (int, float)) else None


def _numbers(fields: dict[str, Any], names: tuple[str, ...]) -> list[float]:
    result: list[float] = []
    for name in names:
        value = _effective(fields, name)
        if isinstance(value, (int, float)):
            result.append(float(value))
    return result


def _maximum_effective(fields: dict[str, Any], names: tuple[str, ...]) -> float | None:
    values = _numbers(fields, names)
    return max(values) if values else None


def _minimum_effective(fields: dict[str, Any], names: tuple[str, ...]) -> float | None:
    values = _numbers(fields, names)
    return min(values) if values else None
