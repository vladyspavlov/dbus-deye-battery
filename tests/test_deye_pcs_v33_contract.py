"""Independent vectors transcribed from the original-layout Deye V3.3 PDF.

These constants deliberately do not derive from ``FAULT_TABLE_BITS``.  The
test is the specification boundary that prevents a self-consistent but
incorrect decoder table from passing again.
"""

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.decoder import DeyeDecoder, FAULT_TABLE_BITS
from deye_virtual_battery.policy import (
    CONDITION_ALARMS,
    UNMAPPED_CONDITION_SEVERITIES,
)
from deye_virtual_battery.profile import DEYE_NATIVE, VICTRON_CAN


# Tuples are ordered bit 0 through bit 7, matching the decoder lookup.
DEYE_V33_FAULT_TABLE_BITS = {
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


# Exact Venus translations, independently reviewed against the meaning of each
# Deye condition and the standard battery-service alarm paths. ``None`` means
# Venus has no honest standard path; the condition must remain diagnostic-only.
EXPECTED_VENUS_ALARMS = {
    "cell_over_voltage": ("/Alarms/HighCellVoltage", 2),
    "cell_under_voltage": ("/Alarms/LowCellVoltage", 2),
    "module_over_voltage": ("/Alarms/HighVoltage", 2),
    "module_under_voltage": ("/Alarms/LowVoltage", 2),
    "charge_over_current": ("/Alarms/HighChargeCurrent", 2),
    "discharge_over_current": ("/Alarms/HighDischargeCurrent", 2),
    "cell_over_temperature_charge": ("/Alarms/HighChargeTemperature", 2),
    "cell_under_temperature_charge": ("/Alarms/LowChargeTemperature", 2),
    "cell_over_temperature_discharge": ("/Alarms/HighTemperature", 2),
    "cell_under_temperature_discharge": ("/Alarms/LowTemperature", 2),
    "cell_voltage_difference_high": ("/Alarms/CellImbalance", 2),
    "cell_temperature_difference_high": (None, 2),
    "mos_over_temperature": ("/Alarms/HighTemperature", 2),
    "heating_film_over_temperature": ("/Alarms/HighTemperature", 2),
    "afe_over_current_discharge_1": ("/Alarms/HighDischargeCurrent", 2),
    "afe_over_current_discharge_2": ("/Alarms/HighDischargeCurrent", 2),
    "afe_under_voltage": ("/Alarms/LowVoltage", 2),
    "afe_over_voltage": ("/Alarms/HighVoltage", 2),
    "afe_over_current_discharge_latched": ("/Alarms/HighDischargeCurrent", 2),
    "afe_over_current_charge": ("/Alarms/HighChargeCurrent", 2),
    "afe_short_circuit_discharge": ("/Alarms/HighDischargeCurrent", 2),
    "afe_under_temperature": ("/Alarms/LowTemperature", 2),
    "afe_over_temperature": ("/Alarms/HighTemperature", 2),
    "afe_short_circuit_discharge_latched": ("/Alarms/HighDischargeCurrent", 2),
    "afe_communication_failure": ("/Alarms/InternalFailure", 2),
    "cell_voltage_sampling_failure": ("/Alarms/InternalFailure", 2),
    "temperature_sampling_failure": ("/Alarms/InternalFailure", 2),
    "mosfet_short_circuit": ("/Alarms/InternalFailure", 2),
    "eeprom_error": ("/Alarms/InternalFailure", 2),
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
    "cell_high_temperature_discharge_warning": ("/Alarms/HighTemperature", 1),
    "cell_low_temperature_discharge_warning": ("/Alarms/LowTemperature", 1),
    "cell_voltage_difference_warning": ("/Alarms/CellImbalance", 1),
    "cell_temperature_difference_warning": (None, 1),
    "mos_high_temperature_warning": ("/Alarms/HighTemperature", 1),
    "heating_film_high_temperature_warning": ("/Alarms/HighTemperature", 1),
    "heating_mos_adhesion": (None, 1),
    "heating_error": (None, 1),
    "connector_high_temperature": ("/Alarms/HighTemperature", 2),
    "precharge_failed": ("/Alarms/Contactor", 2),
    "charge_reversed": ("/Alarms/InternalFailure", 2),
    "terminal_high_temperature": ("/Alarms/HighTemperature", 2),
    "fuse_blown": ("/Alarms/FuseBlown", 2),
    "voltage_open_wire": ("/Alarms/InternalFailure", 2),
    "temperature_open_wire": ("/Alarms/InternalFailure", 2),
    "charge_voltage_low": (None, 2),
}


def frame(can_id: str, payload: str, timestamp: float = 1000.0):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} R")


def values(can_id: str, payload: str, *, profile: str = DEYE_NATIVE):
    decoder = DeyeDecoder(profile_resolver=lambda _timestamp: profile)
    return {
        field.name: field
        for field in decoder.decode(frame(can_id, payload))
    }


def condition_payload(table: int, bit: int, *, mos_flags: int = 0x31) -> str:
    payload = bytearray(8)
    payload[table - 1] = 1 << bit
    payload[7] = mos_flags
    return payload.hex()


def test_fault_table_constant_matches_independent_pdf_transcription():
    assert FAULT_TABLE_BITS == DEYE_V33_FAULT_TABLE_BITS
    assert sum(len(bits) for bits in DEYE_V33_FAULT_TABLE_BITS.values()) == 56


def test_every_one_hot_fault_bit_decodes_identically_in_0x110_and_0x359():
    for table, names in DEYE_V33_FAULT_TABLE_BITS.items():
        for bit, expected in enumerate(names):
            payload = condition_payload(table, bit)
            pack = values("110", payload)["pack.active_v33_conditions"].value
            system = values("359", payload)["alarms.active_v33_conditions"].value
            assert pack == [expected], (table, bit, pack)
            assert system == [expected], (table, bit, system)


def test_every_deye_condition_has_the_reviewed_venus_alarm_translation():
    assert set(EXPECTED_VENUS_ALARMS) == {
        name
        for names in DEYE_V33_FAULT_TABLE_BITS.values()
        for name in names
    }
    for condition, (path, severity) in EXPECTED_VENUS_ALARMS.items():
        if path is None:
            assert condition not in CONDITION_ALARMS
            assert UNMAPPED_CONDITION_SEVERITIES[condition] == severity
        else:
            assert CONDITION_ALARMS[condition] == (path, severity)


def test_0x35c_request_bits_match_the_original_table_columns():
    expected_by_bit = {
        7: "requests.charge_enable_v33",
        6: "requests.discharge_enable_v33",
        5: "requests.force_charge_1",
        4: "requests.force_charge_2",
        3: "requests.full_charge_request",
        0: "requests.request_heat",
    }
    for bit, expected_name in expected_by_bit.items():
        decoded = values("35C", f"{1 << bit:02X}" + "00" * 7)
        assert decoded[expected_name].value is True
        assert decoded["requests.reserved_35c_bits"].value == 0

    decoded = values("35C", "0600000000000000")
    assert decoded["requests.request_heat"].value is False
    assert decoded["requests.reserved_35c_bits"].value == 0x06


def test_0x35e_separates_deye_name_pack_number_and_binary_suffix():
    decoded = values("35E", "44593030311CFC08")
    assert decoded["identity.manufacturer"].value == "DY"
    assert decoded["identity.pack_number"].value == "001"
    assert decoded["identity.frame_35e_wire_name"].value == "DY001"
    assert decoded["identity.cell_manufacturer_code"].value == 0x1C
    assert decoded["battery.installed_capacity"].value == 230.0
    assert decoded["identity.manufacturer_name_conformant"].value is False


def test_0x35e_victron_profile_preserves_wire_name_and_deye_capacity_suffix():
    decoded = values(
        "35E", "50594C4F4E1CFC08", profile=VICTRON_CAN
    )
    assert decoded["identity.manufacturer"].value == "PYLON"
    assert decoded["identity.pack_number"].value is None
    assert decoded["identity.frame_35e_wire_name"].value == "PYLON"
    assert decoded["battery.installed_capacity"].value == 230.0
    assert decoded["identity.manufacturer_name_conformant"].value is False


def test_0x351_uses_source_profile_signedness_before_victron_publication():
    payload = "4802FFFFFEFFE001"
    deye = values("351", payload, profile=DEYE_NATIVE)
    victron = values("351", payload, profile=VICTRON_CAN)

    assert deye["limits.max_charge_current"].value == -0.1
    assert deye["limits.max_discharge_current"].value == -0.2
    assert victron["limits.max_charge_current"].value == 6553.5
    assert victron["limits.max_discharge_current"].value == 6553.4
    assert deye["limits.max_charge_current"].valid is False
    assert victron["limits.max_charge_current"].valid is False

    positive = "48026400FC08E001"
    deye_positive = values("351", positive, profile=DEYE_NATIVE)
    victron_positive = values("351", positive, profile=VICTRON_CAN)
    assert deye_positive["limits.max_charge_current"].value == 10.0
    assert victron_positive["limits.max_charge_current"].value == 10.0
    assert deye_positive["limits.max_discharge_current"].value == 230.0
    assert victron_positive["limits.max_discharge_current"].value == 230.0


def victron_policy_for_0x110(payload: str):
    cache = VirtualBatteryCache()
    cycle = (
        ("35A", "0000000000000000"),
        ("35E", "50594C4F4E1CFC08"),
        ("35F", "001CF005FC084459"),
        ("351", "48026400FC08E001"),
        ("355", "6400640000000000"),
        ("356", "E6140000E6000000"),
        ("110", payload),
    )
    for index, (can_id, frame_payload) in enumerate(cycle):
        cache.apply(frame(can_id, frame_payload, 2000.0 + index * 0.01))
    return build_mock_dbus_model(cache.snapshot(at=2000.1))


def test_corrected_deye_conditions_translate_to_honest_victron_dbus_alarms():
    cases = (
        (condition_payload(7, 4), "/Alarms/FuseBlown", 2),
        (condition_payload(7, 1), "/Alarms/Contactor", 2),
        (condition_payload(2, 0), "/Alarms/HighTemperature", 2),
        (condition_payload(6, 0), "/Alarms/HighTemperature", 1),
    )
    for payload, expected_path, expected_level in cases:
        model = victron_policy_for_0x110(payload)
        assert model["paths"][expected_path] == expected_level
        assert model["paths"]["/Info/MaxChargeCurrent"] == 10.0
        assert model["paths"]["/Info/MaxDischargeCurrent"] == 230.0
        assert model["policy"]["permissions"]["would_directly_switch_inverter_off"] is False
