"""Passive decoder for the observed Deye PCS-CANBUS V3.3 frame family.

The battery can also be switched to a ``victronCAN`` inverter profile.  That
profile keeps the extended Deye diagnostic frames, replaces the Deye vendor
fault frames with Victron's standard ``0x35A``/``0x35F`` pair, and inverts the
``0x356`` current sign.  Frames from both profiles decode here; the active
convention is supplied by :mod:`deye_virtual_battery.profile`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .candump import CanFrame
from .profile import DEYE_NATIVE, VICTRON_CAN


CRITICAL = "critical"
SESSION = "session"
OPTIONAL = "optional"
TRANSPORT = "transport"


class DecodeError(ValueError):
    """Raised when a recognized frame cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class DecodedField:
    name: str
    value: Any
    unit: str | None
    category: str
    source_can_id: int
    valid: bool = True
    confidence: str = "confirmed"
    note: str | None = None


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little", signed=False)


def _i16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little", signed=True)


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little", signed=False)


def _field(
    frame: CanFrame,
    name: str,
    value: Any,
    unit: str | None,
    category: str,
    *,
    valid: bool = True,
    confidence: str = "confirmed",
    note: str | None = None,
) -> DecodedField:
    return DecodedField(
        name=name,
        value=value,
        unit=unit,
        category=category,
        source_can_id=frame.can_id,
        valid=valid,
        confidence=confidence,
        note=note,
    )


def _in_range(value: float, minimum: float, maximum: float) -> bool:
    return minimum <= value <= maximum


def _scaled(value: int, divisor: int, digits: int) -> float:
    """Scale an integer while preserving the protocol's declared precision."""
    return round(value / divisor, digits)


FAULT_TABLE_BITS: dict[int, tuple[str, ...]] = {
    1: (
        "cell_over_voltage",
        "cell_under_voltage",
        "module_over_voltage",
        "module_under_voltage",
        "charge_over_current",
        "discharge_over_current",
        "cell_over_temperature_charge",
        "cell_under_temperature_charge",
    ),
    2: (
        "cell_over_temperature_discharge",
        "cell_under_temperature_discharge",
        "cell_voltage_difference_high",
        "cell_temperature_difference_high",
        "mos_over_temperature",
        "heating_film_over_temperature",
        "afe_over_current_discharge_1",
        "afe_over_current_discharge_2",
    ),
    3: (
        "afe_under_voltage",
        "afe_over_voltage",
        "afe_over_current_discharge_latched",
        "afe_over_current_charge",
        "afe_short_circuit_discharge",
        "afe_under_temperature",
        "afe_over_temperature",
        "afe_short_circuit_discharge_latched",
    ),
    4: (
        "afe_communication_failure",
        "cell_voltage_sampling_failure",
        "temperature_sampling_failure",
        "mosfet_short_circuit",
        "eeprom_error",
        "internal_communication_failure",
        "pcs_communication_failure",
        "master_address_duplicate",
    ),
    5: (
        "cell_high_voltage_warning",
        "cell_low_voltage_warning",
        "module_high_voltage_warning",
        "module_low_voltage_warning",
        "charge_high_current_warning",
        "discharge_high_current_warning",
        "cell_high_temperature_charge_warning",
        "cell_low_temperature_charge_warning",
    ),
    6: (
        "cell_high_temperature_discharge_warning",
        "cell_low_temperature_discharge_warning",
        "cell_voltage_difference_warning",
        "cell_temperature_difference_warning",
        "mos_high_temperature_warning",
        "heating_film_high_temperature_warning",
        "heating_mos_adhesion",
        "heating_error",
    ),
    7: (
        "connector_high_temperature",
        "precharge_failed",
        "charge_reversed",
        "terminal_high_temperature",
        "fuse_blown",
        "voltage_open_wire",
        "temperature_open_wire",
        "charge_voltage_low",
    ),
}


def _active_table_bits(data: bytes) -> list[str]:
    active: list[str] = []
    for table_number, raw in enumerate(data[:7], 1):
        names = FAULT_TABLE_BITS[table_number]
        active.extend(name for bit, name in enumerate(names) if raw & (1 << bit))
    return active


def _ascii(data: bytes) -> tuple[str | None, bool]:
    printable = all(32 <= byte <= 126 for byte in data)
    return (data.decode("ascii") if printable else None, printable)


# Victron's 0x35A warning/alarm frame.  Each entry is two bits, indexed from
# the least significant bit of the byte, and the two-bit value is a tri-state:
# 0b01 means active, 0b10 means inactive, and both 0b00 and 0b11 mean the BMS
# does not support that field.  A zero byte therefore means "not supported",
# never "OK"; the live Deye victronCAN profile sends eight zero bytes.
VICTRON_ALARM_NOT_SUPPORTED = "not_supported"
VICTRON_ALARM_ACTIVE = "active"
VICTRON_ALARM_INACTIVE = "inactive"

VICTRON_ALARM_STATES = {
    0b00: VICTRON_ALARM_NOT_SUPPORTED,
    0b01: VICTRON_ALARM_ACTIVE,
    0b10: VICTRON_ALARM_INACTIVE,
    0b11: VICTRON_ALARM_NOT_SUPPORTED,
}

# (byte, bit offset, field name, Venus alarm path or None, severity)
VICTRON_ALARM_BITS: tuple[tuple[int, int, str, str | None, int], ...] = (
    (0, 0, "general_alarm", None, 2),
    (0, 2, "high_voltage_alarm", "/Alarms/HighVoltage", 2),
    (0, 4, "low_voltage_alarm", "/Alarms/LowVoltage", 2),
    (0, 6, "high_temperature_alarm", "/Alarms/HighTemperature", 2),
    (1, 0, "low_temperature_alarm", "/Alarms/LowTemperature", 2),
    (1, 2, "high_temperature_charge_alarm", "/Alarms/HighChargeTemperature", 2),
    (1, 4, "low_temperature_charge_alarm", "/Alarms/LowChargeTemperature", 2),
    (1, 6, "high_current_alarm", "/Alarms/HighDischargeCurrent", 2),
    (2, 0, "high_charge_current_alarm", "/Alarms/HighChargeCurrent", 2),
    (2, 2, "contactor_alarm", "/Alarms/Contactor", 2),
    (2, 4, "short_circuit_alarm", None, 2),
    (2, 6, "bms_internal_alarm", "/Alarms/InternalFailure", 2),
    (3, 0, "cell_imbalance_alarm", "/Alarms/CellImbalance", 2),
    (4, 0, "general_warning", None, 1),
    (4, 2, "high_voltage_warning", "/Alarms/HighVoltage", 1),
    (4, 4, "low_voltage_warning", "/Alarms/LowVoltage", 1),
    (4, 6, "high_temperature_warning", "/Alarms/HighTemperature", 1),
    (5, 0, "low_temperature_warning", "/Alarms/LowTemperature", 1),
    (5, 2, "high_temperature_charge_warning", "/Alarms/HighChargeTemperature", 1),
    (5, 4, "low_temperature_charge_warning", "/Alarms/LowChargeTemperature", 1),
    (5, 6, "high_current_warning", "/Alarms/HighDischargeCurrent", 1),
    (6, 0, "high_charge_current_warning", "/Alarms/HighChargeCurrent", 1),
    (6, 2, "contactor_warning", "/Alarms/Contactor", 1),
    (6, 4, "short_circuit_warning", None, 1),
    (6, 6, "bms_internal_warning", "/Alarms/InternalFailure", 1),
    (7, 0, "cell_imbalance_warning", "/Alarms/CellImbalance", 1),
)

#: Venus alarm path and severity for each supported 0x35A field name.
VICTRON_ALARM_PATHS: dict[str, tuple[str, int]] = {
    name: (path, severity)
    for _, _, name, path, severity in VICTRON_ALARM_BITS
    if path is not None
}


def decode_victron_alarm_frame(payload: bytes) -> dict[str, str]:
    """Return the tri-state value of every 0x35A field."""

    return {
        name: VICTRON_ALARM_STATES[(payload[byte] >> offset) & 0b11]
        for byte, offset, name, _, _ in VICTRON_ALARM_BITS
    }


class DeyeDecoder:
    """Decode V3.3 fields without sending CAN or publishing to D-Bus.

    Names follow the preserved V3.3 translation, with raw values retained when
    formatting or enum labels remain unknown. Charge-related signals stay
    independent because the live SE-F12-C has emitted contradictory summaries.
    """

    BATTERY_IDS = {
        0x110, 0x150, 0x200, 0x250, 0x351, 0x355, 0x356, 0x358, 0x359,
        0x35A, 0x35C, 0x35E, 0x35F, 0x361, 0x363, 0x364, 0x371, 0x400,
        0x500, 0x550, 0x600, 0x650, 0x700, 0x750,
    }
    INVERTER_IDS = {0x305, 0x306, 0x307}

    def __init__(
        self, profile_resolver: Callable[[float], str] | None = None
    ) -> None:
        """Decode frames, optionally consulting a live profile detector.

        Without a resolver the decoder assumes the Deye native profile, which
        is how the battery is configured by default and how every capture
        recorded before 2026-09-01 was produced.
        """

        self._profile_resolver = profile_resolver

    def profile_at(self, timestamp: float) -> str:
        if self._profile_resolver is None:
            return DEYE_NATIVE
        return self._profile_resolver(timestamp)

    def decode(self, frame: CanFrame) -> list[DecodedField]:
        if frame.can_id in self.BATTERY_IDS and frame.direction == "T":
            return []
        if frame.can_id in self.INVERTER_IDS and frame.direction == "R":
            return []
        handler = getattr(self, f"_decode_{frame.can_id:03x}", None)
        if handler is None:
            return []
        if frame.dlc != 8:
            raise DecodeError(
                f"0x{frame.can_id:03X}: expected DLC 8, received {frame.dlc}"
            )
        return handler(frame)

    def _decode_305(self, frame: CanFrame) -> list[DecodedField]:
        expected = frame.payload == bytes(8)
        return [
            _field(frame, "transport.request_305_seen", True, None, TRANSPORT),
            _field(
                frame,
                "transport.request_305_payload_valid",
                expected,
                None,
                TRANSPORT,
                valid=expected,
                note="Deye V1.0 requires eight zero bytes",
            ),
        ]

    def _decode_307(self, frame: CanFrame) -> list[DecodedField]:
        expected = frame.payload == bytes.fromhex("1234567856494300")
        return [
            _field(frame, "transport.identity_307_seen", True, None, TRANSPORT),
            _field(
                frame,
                "transport.identity_307_payload_valid",
                expected,
                None,
                TRANSPORT,
                valid=expected,
                confidence="high",
                note=(
                    "Legacy Victron inverter-identification frame; fixed payload "
                    "corroborated by Victron release notes and public captures; "
                    "not a Deye battery measurement"
                ),
            ),
        ]

    def _decode_306(self, frame: CanFrame) -> list[DecodedField]:
        timeout_seconds = _u16(frame.payload, 1)
        return [
            _field(frame, "transport.usb_board_enable", bool(frame.payload[0]), None, TRANSPORT),
            _field(frame, "transport.usb_board_disable_timeout", timeout_seconds, "s", TRANSPORT, valid=timeout_seconds <= 18000),
        ]

    def _decode_351(self, frame: CanFrame) -> list[DecodedField]:
        # Deye V3.3 declares CCL and DCL as signed 16-bit values, while
        # Victron's controlling LV protocol declares them unsigned and expects
        # positive values.  The SE-F12-C sends ordinary non-negative values in
        # both profiles, for which the encodings are identical.  Decode using
        # the active wire profile so malformed/high-bit values are rejected
        # according to the specification that produced them.
        profile = self.profile_at(frame.timestamp)
        current_limit_word = _u16 if profile == VICTRON_CAN else _i16
        cvl = _scaled(_u16(frame.payload, 0), 10, 1)
        ccl = _scaled(current_limit_word(frame.payload, 2), 10, 1)
        dcl = _scaled(current_limit_word(frame.payload, 4), 10, 1)
        low = _scaled(_u16(frame.payload, 6), 10, 1)
        return [
            _field(frame, "limits.max_charge_voltage", cvl, "V", CRITICAL, valid=_in_range(cvl, 40, 65)),
            _field(frame, "limits.max_charge_current", ccl, "A", CRITICAL, valid=_in_range(ccl, 0, 1000)),
            _field(frame, "limits.max_discharge_current", dcl, "A", CRITICAL, valid=_in_range(dcl, 0, 1000)),
            _field(
                frame,
                "limits.battery_low_voltage",
                low,
                "V",
                CRITICAL,
                valid=_in_range(low, 35, 60),
                note=(
                    "Discharge voltage limit; Venus does not act on it, the "
                    "installer configures the DC low disconnect voltages "
                    "instead"
                ),
            ),
            _field(
                frame,
                "diagnostics.high_cvl_zero_ccl",
                ccl <= 0.0 and cvl > 57.6,
                None,
                CRITICAL,
                note="Flags the observed combination; it does not alter limits",
            ),
        ]

    def _decode_355(self, frame: CanFrame) -> list[DecodedField]:
        soc = float(_u16(frame.payload, 0))
        soh = float(_u16(frame.payload, 2))
        # Bytes 4-5 carry an optional high-resolution SOC.  Venus does not
        # consume it and the observed battery sends zero in both profiles, so
        # it is recorded as a diagnostic and never published.
        high_resolution_soc = _scaled(_u16(frame.payload, 4), 100, 2)
        return [
            _field(frame, "battery.soc", soc, "%", CRITICAL, valid=_in_range(soc, 0, 100)),
            _field(frame, "battery.soh", soh, "%", CRITICAL, valid=_in_range(soh, 0, 100)),
            _field(
                frame,
                "diagnostics.high_resolution_soc_355",
                high_resolution_soc,
                "%",
                OPTIONAL,
                valid=_in_range(high_resolution_soc, 0, 100),
                note="Not consumed by Venus; recorded as a diagnostic only",
            ),
        ]

    def _decode_356(self, frame: CanFrame) -> list[DecodedField]:
        # Both the supplied Victron profile and Deye V3.3 declare this field as
        # signed 16-bit.  Valid battery voltage remains positive, but decoding
        # the declared type lets the range check reject a malformed negative
        # value instead of misreading it as a very large positive voltage.
        voltage = _scaled(_i16(frame.payload, 0), 100, 2)
        wire_current = _scaled(_i16(frame.payload, 2), 10, 1)
        temperature = _scaled(_i16(frame.payload, 4), 10, 1)
        # The sign of this field depends on the BMS-side protocol profile, so
        # the Victron-convention current and power are derived from the cached
        # wire value at snapshot time rather than fixed here.  See
        # ``VirtualBatteryCache.snapshot`` and :mod:`deye_virtual_battery.profile`.
        return [
            _field(frame, "battery.voltage", voltage, "V", CRITICAL, valid=_in_range(voltage, 35, 65)),
            _field(
                frame,
                "battery.current_raw_deye",
                wire_current,
                "A",
                CRITICAL,
                valid=_in_range(wire_current, -1000, 1000),
                confidence="confirmed-observation",
                note=(
                    "Raw 0x356 wire value; positive was observed while the "
                    "battery was discharging in the Deye native profile"
                ),
            ),
            _field(frame, "battery.temperature", temperature, "C", CRITICAL, valid=_in_range(temperature, -40, 90)),
        ]

    def _decode_359(self, frame: CanFrame) -> list[DecodedField]:
        active = _active_table_bits(frame.payload)
        marker_bytes = frame.payload[5:7]
        marker = marker_bytes.decode("ascii", errors="replace")
        pn_present = marker_bytes == b"PN"
        fields = [
            _field(frame, "alarms.raw_alarm_word", _u16(frame.payload, 0), None, CRITICAL, note="Legacy v0.71 grouping; V3.3 uses per-byte tables"),
            _field(frame, "alarms.raw_warning_word", _u16(frame.payload, 2), None, CRITICAL, note="Legacy v0.71 grouping; V3.3 uses per-byte tables"),
            _field(frame, "alarms.active_v33_conditions", active, None, CRITICAL),
            _field(frame, "alarms.any_v33_condition_active", bool(active), None, CRITICAL),
            _field(frame, "identity.frame_359_marker", marker, None, SESSION),
            _field(frame, "diagnostics.pylon_pn_marker_present", pn_present, None, SESSION),
            _field(
                frame,
                "diagnostics.stock_victron_would_select_lg",
                not pn_present,
                None,
                SESSION,
                note="Proven for installed can-bus-bms v0.71 only",
            ),
        ]
        fields.extend(
            _field(frame, f"alarms.table_{number}_raw", raw, None, CRITICAL)
            for number, raw in enumerate(frame.payload[:7], 1)
        )
        return fields

    def _decode_35c(self, frame: CanFrame) -> list[DecodedField]:
        flags = frame.payload[0]
        return [
            _field(frame, "requests.raw_35c_flags", flags, None, CRITICAL),
            _field(frame, "requests.charge_enable_v33", bool(flags & 0x80), None, CRITICAL, note="Do not use alone as charge permission"),
            _field(frame, "requests.discharge_enable_v33", bool(flags & 0x40), None, CRITICAL, note="Do not use alone as discharge permission"),
            _field(frame, "requests.force_charge_1", bool(flags & 0x20), None, CRITICAL),
            _field(frame, "requests.force_charge_2", bool(flags & 0x10), None, CRITICAL),
            _field(frame, "requests.full_charge_request", bool(flags & 0x08), None, CRITICAL),
            _field(frame, "requests.request_heat", bool(flags & 0x01), None, CRITICAL),
            _field(frame, "requests.reserved_35c_bits", flags & 0x06, None, CRITICAL),
        ]

    def _decode_35a(self, frame: CanFrame) -> list[DecodedField]:
        """Decode Victron's standard warning/alarm frame.

        Only the ``victronCAN`` battery profile sends this frame.  The live
        SE-F12-C sends eight zero bytes, which Victron's specification defines
        as "not supported" for every field rather than "OK".  The Deye
        condition tables in ``0x110`` therefore remain the real alarm source.
        """

        states = decode_victron_alarm_frame(frame.payload)
        supported = sorted(
            name for name, state in states.items()
            if state != VICTRON_ALARM_NOT_SUPPORTED
        )
        active = sorted(
            name for name, state in states.items() if state == VICTRON_ALARM_ACTIVE
        )
        system_status = (frame.payload[7] >> 2) & 0b11
        fields = [
            _field(frame, "victron_alarms.frame_35a_seen", True, None, CRITICAL),
            _field(frame, "victron_alarms.supported_fields", supported, None, CRITICAL),
            _field(frame, "victron_alarms.active_fields", active, None, CRITICAL),
            _field(
                frame,
                "victron_alarms.any_supported_field",
                bool(supported),
                None,
                CRITICAL,
                note=(
                    "All-zero 0x35A means every alarm is unsupported, not OK; "
                    "the live SE-F12-C victronCAN profile reports no alarms"
                ),
            ),
            _field(
                frame,
                "victron_alarms.system_status_raw",
                system_status,
                None,
                CRITICAL,
                note="0x35A byte 7 bits 2-3; Victron documents no value table",
            ),
            _field(frame, "victron_alarms.raw_bytes", frame.payload.hex().upper(), None, CRITICAL),
        ]
        fields.extend(
            _field(frame, f"victron_alarms.{name}", state, None, CRITICAL)
            for name, state in sorted(states.items())
        )
        return fields

    def _decode_35f(self, frame: CanFrame) -> list[DecodedField]:
        """Decode Victron's battery type and software-version frame.

        Bytes 4-5 carry usable/online capacity in whole amp
        hours.  The live victronCAN profile sends ``2300`` there, which is the
        Deye 0.1 Ah encoding of the same 230 Ah pack.  The value is preserved
        raw and marked conflicting; the adapter must not publish it as Ah.
        """

        model_raw = frame.payload[:2].hex().upper()
        firmware_big_endian = int.from_bytes(frame.payload[2:4], "big")
        firmware_little_endian = _u16(frame.payload, 2)
        capacity_raw = _u16(frame.payload, 4)
        suffix_text, suffix_printable = _ascii(frame.payload[6:])
        deye_scaled_capacity = _scaled(capacity_raw, 10, 1)
        conflicting = capacity_raw > 2000
        return [
            _field(frame, "victron_identity.frame_35f_seen", True, None, SESSION),
            _field(frame, "victron_identity.battery_model_raw", model_raw, None, SESSION,
                   note="Victron marks the type ID as not implemented"),
            _field(frame, "victron_identity.firmware_version_big_endian", firmware_big_endian, None, SESSION,
                   note="MSB-first ordering; display formatting is undocumented"),
            _field(frame, "victron_identity.firmware_version_little_endian", firmware_little_endian, None, SESSION,
                   note="Matches the Deye 0x363/0x500 version word"),
            _field(frame, "victron_identity.online_capacity_raw", capacity_raw, None, SESSION),
            _field(
                frame,
                "victron_identity.online_capacity_ah_victron_scaling",
                float(capacity_raw),
                "Ah",
                SESSION,
                valid=not conflicting,
                confidence="conflicting" if conflicting else "confirmed",
                note=(
                    "Victron scaling is 1 Ah; the live value equals ten times "
                    "the 230 Ah pack, so it must not be published as capacity"
                ),
            ),
            _field(
                frame,
                "victron_identity.online_capacity_ah_deye_scaling",
                deye_scaled_capacity,
                "Ah",
                SESSION,
                confidence="high",
                note="0.1 Ah scaling reproduces the 230 Ah 0x35E nominal capacity",
            ),
            _field(frame, "victron_identity.frame_35f_suffix", suffix_text, None, SESSION,
                   valid=suffix_printable,
                   note="Undocumented in Victron's table; the live battery sends 'DY'"),
        ]

    def _decode_35e(self, frame: CanFrame) -> list[DecodedField]:
        wire_name, wire_name_printable = _ascii(frame.payload[:5])
        capacity = _scaled(_u16(frame.payload, 6), 10, 1)
        # 0x35E is defined as eight 7-bit ASCII characters.  The live
        # battery pads it with binary capacity bytes in both profiles, so the
        # conformance of the full field is reported separately from the name.
        name_conformant = all(
            byte == 0 or 32 <= byte <= 126 for byte in frame.payload
        )
        victron_profile = frame.payload.startswith(b"PYLON")
        deye_profile = frame.payload.startswith(b"DY")
        if victron_profile:
            # The selected battery profile puts the five-byte PYLON name on
            # the wire, followed by the Deye cell-maker/capacity suffix.
            manufacturer = wire_name
            manufacturer_printable = wire_name_printable
            pack_number = None
            pack_number_printable = True
            protocol_family = VICTRON_CAN
        elif deye_profile:
            # In the original-layout Deye V3.3 table, bytes 0-1 are the
            # manufacturer abbreviation and bytes 2-4 are the ASCII battery
            # pack number.  The live payload is therefore DY + 001, not one
            # five-character manufacturer name.
            manufacturer, manufacturer_printable = _ascii(frame.payload[:2])
            pack_number, pack_number_printable = _ascii(frame.payload[2:5])
            protocol_family = DEYE_NATIVE
        else:
            # Preserve an otherwise conformant Victron name without inventing
            # a Deye pack-number split for an unknown manufacturer.
            full_name, full_name_printable = _ascii(frame.payload)
            manufacturer = (
                full_name.rstrip("\x00 ") if full_name is not None else None
            )
            manufacturer_printable = full_name_printable
            pack_number = None
            pack_number_printable = True
            protocol_family = self.profile_at(frame.timestamp)
        return [
            _field(
                frame,
                "identity.manufacturer",
                manufacturer,
                None,
                SESSION,
                valid=manufacturer_printable,
            ),
            _field(
                frame,
                "identity.manufacturer_name_conformant",
                name_conformant,
                None,
                SESSION,
                note="Victron requires eight 7-bit ASCII characters in 0x35E",
            ),
            _field(
                frame,
                "identity.frame_35e_wire_name",
                wire_name,
                None,
                SESSION,
                valid=wire_name_printable,
                note="First five wire bytes retained for profile evidence",
            ),
            _field(frame, "identity.protocol_family", protocol_family, None, SESSION),
            _field(
                frame,
                "identity.pack_number",
                pack_number,
                None,
                SESSION,
                valid=pack_number_printable,
                note="Deye V3.3 bytes 2-4; not applicable to the victronCAN name layout",
            ),
            _field(
                frame,
                "identity.pack_number_raw",
                frame.payload[2:5].hex().upper() if deye_profile else None,
                None,
                SESSION,
            ),
            _field(frame, "identity.cell_manufacturer_code", frame.payload[5], None, SESSION, note="Live 0x1C is absent from the V3.3 code list"),
            _field(
                frame,
                "battery.installed_capacity",
                capacity,
                "Ah",
                SESSION,
                valid=_in_range(capacity, 1, 2000),
                confidence="confirmed-observation",
                note=(
                    "Deye V3.3 bytes 6-7 at 0.1 Ah; retained in victronCAN "
                    "despite conflicting with Victron's eight-character 0x35E"
                ),
            ),
            _field(frame, "identity.frame_35e_binary_suffix", frame.payload[5:].hex().upper(), None, SESSION),
            _field(
                frame,
                "identity.victron_profile_marker",
                victron_profile,
                None,
                SESSION,
                note=(
                    "The victronCAN profile renames the battery to PYLON on "
                    "the wire; the adapter still publishes its own identity"
                ),
            ),
        ]

    def _decode_150(self, frame: CanFrame) -> list[DecodedField]:
        voltage = _scaled(_u16(frame.payload, 0), 10, 1)
        raw_deye_current = _scaled(_i16(frame.payload, 2), 10, 1)
        victron_current = -raw_deye_current
        soc = _scaled(_u16(frame.payload, 4), 10, 1)
        soh = _scaled(_u16(frame.payload, 6), 10, 1)
        note = "SE-F12-C observation; absent from public Deye V1.0"
        return [
            _field(frame, "diagnostics.voltage_150", voltage, "V", OPTIONAL, valid=_in_range(voltage, 35, 65), confidence="high", note=note),
            _field(frame, "diagnostics.current_raw_deye_150", raw_deye_current, "A", OPTIONAL, valid=_in_range(raw_deye_current, -1000, 1000), confidence="high", note=note),
            _field(frame, "diagnostics.current_victron_150", victron_current, "A", OPTIONAL, valid=_in_range(victron_current, -1000, 1000), confidence="high", note="Deye sign inverted for Victron convention"),
            _field(frame, "diagnostics.soc_150", soc, "%", OPTIONAL, valid=_in_range(soc, 0, 100), confidence="high", note=note),
            _field(frame, "diagnostics.soh_150", soh, "%", OPTIONAL, valid=_in_range(soh, 0, 100), confidence="high", note=note),
        ]

    def _decode_110(self, frame: CanFrame) -> list[DecodedField]:
        active = _active_table_bits(frame.payload)
        flags = frame.payload[7]
        fields = [
            _field(frame, "pack.active_v33_conditions", active, None, CRITICAL),
            _field(frame, "pack.any_v33_condition_active", bool(active), None, CRITICAL),
            _field(frame, "mos.raw_flags", flags, None, CRITICAL),
            _field(frame, "mos.parallel_complete", bool(flags & 0x01), None, CRITICAL),
            _field(frame, "mos.charge_closed", bool(flags & 0x10), None, CRITICAL),
            _field(frame, "mos.discharge_closed", bool(flags & 0x20), None, CRITICAL),
            _field(frame, "mos.precharge_closed", bool(flags & 0x40), None, CRITICAL),
            _field(frame, "mos.heater_closed", bool(flags & 0x80), None, CRITICAL),
            _field(frame, "mos.reserved_flags", flags & 0x0E, None, CRITICAL),
        ]
        fields.extend(
            _field(frame, f"pack.condition_table_{number}_raw", raw, None, CRITICAL)
            for number, raw in enumerate(frame.payload[:7], 1)
        )
        return fields

    def _decode_200(self, frame: CanFrame) -> list[DecodedField]:
        return self._decode_cell_extrema(frame)

    def _decode_250(self, frame: CanFrame) -> list[DecodedField]:
        mos_temperature = _scaled(_i16(frame.payload, 0), 10, 1)
        heating_temperature = _scaled(_i16(frame.payload, 2), 10, 1)
        max_charge = float(_u16(frame.payload, 4))
        max_discharge = float(_u16(frame.payload, 6))
        return [
            _field(frame, "pack.maximum_mos_temperature", mos_temperature, "C", OPTIONAL, valid=_in_range(mos_temperature, -40, 120)),
            _field(frame, "pack.heating_film_temperature", heating_temperature, "C", OPTIONAL, valid=_in_range(heating_temperature, -40, 120), note="-40 C may be a sentinel when no heater sensor is present"),
            _field(frame, "pack.maximum_allowable_charge_current", max_charge, "A", OPTIONAL, valid=_in_range(max_charge, 0, 1000)),
            _field(frame, "pack.maximum_allowable_discharge_current", max_discharge, "A", OPTIONAL, valid=_in_range(max_discharge, 0, 1000)),
        ]

    def _decode_361(self, frame: CanFrame) -> list[DecodedField]:
        return self._decode_cell_extrema(frame)

    def _decode_358(self, frame: CanFrame) -> list[DecodedField]:
        realtime_power = _scaled(_u16(frame.payload, 0), 10, 1)
        cumulative_energy = _scaled(_u32(frame.payload, 2), 10, 1)
        return [
            _field(frame, "usb.realtime_power", realtime_power, "W", OPTIONAL),
            _field(frame, "usb.cumulative_energy", cumulative_energy, "Wh", OPTIONAL),
            _field(frame, "usb.switch_enabled", bool(frame.payload[6]), None, OPTIONAL),
            _field(frame, "usb.reserved_byte_7", frame.payload[7], None, OPTIONAL),
        ]

    def _decode_363(self, frame: CanFrame) -> list[DecodedField]:
        return [
            _field(frame, "identity.host_software_version_raw", _u16(frame.payload, 0), None, SESSION, note="V3.3 does not fully specify display formatting"),
            _field(frame, "identity.host_hardware_version_raw", _u16(frame.payload, 2), None, SESSION, note="V3.3 does not fully specify display formatting"),
            _field(frame, "identity.host_version_reserved", frame.payload[4:].hex().upper(), None, SESSION),
        ]

    def _decode_364(self, frame: CanFrame) -> list[DecodedField]:
        return [
            _field(frame, "modules.normal", frame.payload[0], None, CRITICAL),
            _field(frame, "modules.charge_disabled", frame.payload[1], None, CRITICAL, note="Do not use alone as charge permission on SE-F12-C"),
            _field(frame, "modules.discharge_disabled", frame.payload[2], None, CRITICAL),
            _field(frame, "modules.communication_disconnected", frame.payload[3], None, CRITICAL),
            _field(frame, "modules.parallel_connected", frame.payload[4], None, CRITICAL),
            _field(frame, "modules.reserved_364", frame.payload[5:].hex().upper(), None, CRITICAL),
        ]

    def _decode_371(self, frame: CanFrame) -> list[DecodedField]:
        max_charge = _scaled(_i16(frame.payload, 0), 10, 1)
        max_discharge = _scaled(_i16(frame.payload, 2), 10, 1)
        return [
            _field(frame, "limits.array_max_charge_current", max_charge, "A", CRITICAL, valid=_in_range(max_charge, 0, 1000)),
            _field(frame, "limits.array_max_discharge_current", max_discharge, "A", CRITICAL, valid=_in_range(max_discharge, 0, 1000)),
            _field(frame, "limits.reserved_371", frame.payload[4:].hex().upper(), None, CRITICAL),
        ]

    def _decode_400(self, frame: CanFrame) -> list[DecodedField]:
        mode = frame.payload[0]
        fault_level = frame.payload[1]
        cycles = _u16(frame.payload, 2)
        balance_mask = _u16(frame.payload, 4)
        substate = _u16(frame.payload, 6)
        mode_names = {0: "standstill", 1: "charge", 2: "discharge"}
        fault_names = {0: "none", 1: "minor", 2: "major"}
        return [
            _field(frame, "system.operation_mode_code", mode, None, CRITICAL, valid=mode in mode_names),
            _field(frame, "system.operation_mode", mode_names.get(mode, "unknown"), None, CRITICAL, valid=mode in mode_names),
            _field(frame, "system.fault_level_code", fault_level, None, CRITICAL, valid=fault_level in fault_names),
            _field(frame, "system.fault_level", fault_names.get(fault_level, "unknown"), None, CRITICAL, valid=fault_level in fault_names),
            _field(frame, "battery.cycle_count", cycles, None, OPTIONAL),
            _field(frame, "cells.balance_mask", balance_mask, None, OPTIONAL),
            _field(frame, "cells.balancing", [cell for cell in range(1, 17) if balance_mask & (1 << (cell - 1))], None, OPTIONAL),
            _field(frame, "system.substate_raw", substate, None, CRITICAL, note="V3.3 defines the field but does not enumerate values"),
        ]

    def _decode_cell_extrema(self, frame: CanFrame) -> list[DecodedField]:
        maximum_voltage = _scaled(_u16(frame.payload, 0), 1000, 3)
        minimum_voltage = _scaled(_u16(frame.payload, 2), 1000, 3)
        maximum_temperature = _scaled(_i16(frame.payload, 4), 10, 1)
        minimum_temperature = _scaled(_i16(frame.payload, 6), 10, 1)
        suffix = f"_{frame.can_id:03x}"
        note = "Observed duplicate extended frame; diagnostic only"
        return [
            _field(frame, f"cells.max_voltage{suffix}", maximum_voltage, "V", OPTIONAL, valid=_in_range(maximum_voltage, 2, 4.5), confidence="high", note=note),
            _field(frame, f"cells.min_voltage{suffix}", minimum_voltage, "V", OPTIONAL, valid=_in_range(minimum_voltage, 2, 4.5), confidence="high", note=note),
            _field(frame, f"cells.max_temperature{suffix}", maximum_temperature, "C", OPTIONAL, valid=_in_range(maximum_temperature, -40, 90), confidence="medium-high", note=note),
            _field(frame, f"cells.min_temperature{suffix}", minimum_temperature, "C", OPTIONAL, valid=_in_range(minimum_temperature, -40, 90), confidence="medium-high", note=note),
        ]

    def _decode_500(self, frame: CanFrame) -> list[DecodedField]:
        boot_version, printable = _ascii(frame.payload[3:8])
        marker_valid = frame.payload[2] == 0xAA
        return [
            _field(frame, "identity.pack_software_version_raw", _u16(frame.payload, 0), None, SESSION),
            _field(frame, "identity.boot_version_marker_valid", marker_valid, None, SESSION, valid=marker_valid),
            _field(frame, "identity.boot_version", boot_version, None, SESSION, valid=printable),
        ]

    def _decode_550(self, frame: CanFrame) -> list[DecodedField]:
        return [
            _field(frame, "history.charged_energy", _scaled(_u32(frame.payload, 0), 1000, 3), "kWh", OPTIONAL),
            _field(frame, "history.discharged_energy", _scaled(_u32(frame.payload, 4), 1000, 3), "kWh", OPTIONAL),
        ]

    def _decode_600(self, frame: CanFrame) -> list[DecodedField]:
        return self._decode_serial_half(frame, "first")

    def _decode_650(self, frame: CanFrame) -> list[DecodedField]:
        return self._decode_serial_half(frame, "second")

    def _decode_serial_half(self, frame: CanFrame, half: str) -> list[DecodedField]:
        value, printable = _ascii(frame.payload)
        return [
            _field(frame, f"identity.serial_{half}_half", value, None, SESSION, valid=printable, note="Sensitive local-only identifier; redact from shared output")
        ]

    def _decode_700(self, frame: CanFrame) -> list[DecodedField]:
        names = ("overcharge_count", "overdischarge_count", "short_circuit_count", "mos_over_temperature_count")
        return [
            _field(frame, f"history.{name}", _u16(frame.payload, offset), None, OPTIONAL)
            for name, offset in zip(names, range(0, 8, 2), strict=True)
        ]

    def _decode_750(self, frame: CanFrame) -> list[DecodedField]:
        names = (
            "charge_overcurrent_count",
            "discharge_overcurrent_count",
            "charge_over_temperature_count",
            "discharge_over_temperature_count",
        )
        return [
            _field(frame, f"history.{name}", _u16(frame.payload, offset), None, OPTIONAL)
            for name, offset in zip(names, range(0, 8, 2), strict=True)
        ]
