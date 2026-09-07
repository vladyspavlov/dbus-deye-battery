from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import CandumpParseError, parse_candump_line
from deye_virtual_battery.decoder import DeyeDecoder


def frame(can_id: str, payload: str, timestamp: float = 1000.0, direction: str = "R"):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} {direction}")


def values(can_frame):
    return {field.name: field for field in DeyeDecoder().decode(can_frame)}


def test_candump_parser_preserves_direction_and_bytes():
    parsed = frame("351", "48020000FC08E001")
    assert parsed.can_id == 0x351
    assert parsed.dlc == 8
    assert parsed.payload == bytes.fromhex("48020000FC08E001")
    assert parsed.direction == "R"


def test_candump_parser_rejects_invalid_lines():
    try:
        parse_candump_line("not a CAN frame")
    except CandumpParseError:
        pass
    else:
        raise AssertionError("invalid line should have failed")


def test_351_operational_limits_and_integration_warning():
    decoded = values(frame("351", "48020000FC08E001"))
    assert decoded["limits.max_charge_voltage"].value == 58.4
    assert decoded["limits.max_charge_current"].value == 0.0
    assert decoded["limits.max_discharge_current"].value == 230.0
    assert decoded["limits.battery_low_voltage"].value == 48.0
    assert decoded["diagnostics.high_cvl_zero_ccl"].value is True


def test_356_signed_current_and_measurements():
    decoded = values(frame("356", "E614F4FFE6000000"))
    assert decoded["battery.voltage"].value == 53.5
    assert decoded["battery.current_raw_deye"].value == -1.2
    assert decoded["battery.temperature"].value == 23.0
    # The Victron-convention current is profile dependent and is therefore
    # derived from the cached wire value; see tests/test_profile.py.
    assert "battery.current" not in decoded


def test_observed_inverter_only_payload_keeps_the_raw_wire_sign():
    decoded = values(frame("356", "D2140E00DC000000"))
    assert decoded["battery.current_raw_deye"].value == 1.4


def test_359_all_zero_exposes_installed_decoder_identity_hazard():
    decoded = values(frame("359", "0000000000000000"))
    assert decoded["alarms.raw_alarm_word"].value == 0
    assert decoded["diagnostics.pylon_pn_marker_present"].value is False
    assert decoded["diagnostics.stock_victron_would_select_lg"].value is True


def test_359_pn_marker_is_detected_without_claiming_deye_is_pylon():
    decoded = values(frame("359", "0000000000504E00"))
    assert decoded["identity.frame_359_marker"].value == "PN"
    assert decoded["diagnostics.pylon_pn_marker_present"].value is True
    assert decoded["diagnostics.stock_victron_would_select_lg"].value is False


def test_35c_v33_flags_are_decoded_but_not_collapsed_into_policy():
    decoded = values(frame("35C", "4000000000000000"))
    assert decoded["requests.raw_35c_flags"].value == 0x40
    assert decoded["requests.charge_enable_v33"].value is False
    assert decoded["requests.discharge_enable_v33"].value is True
    assert decoded["requests.full_charge_request"].value is False
    assert decoded["requests.reserved_35c_bits"].value == 0

    heat = values(frame("35C", "0100000000000000"))
    assert heat["requests.request_heat"].value is True
    assert heat["requests.reserved_35c_bits"].value == 0

    reserved = values(frame("35C", "0600000000000000"))
    assert reserved["requests.request_heat"].value is False
    assert reserved["requests.reserved_35c_bits"].value == 0x06


def test_35e_stops_text_at_binary_suffix():
    decoded = values(frame("35E", "44593030311CFC08"))
    assert decoded["identity.manufacturer"].value == "DY"
    assert decoded["identity.frame_35e_wire_name"].value == "DY001"
    assert decoded["identity.pack_number"].value == "001"
    assert decoded["identity.pack_number_raw"].value == "303031"
    assert decoded["identity.frame_35e_binary_suffix"].value == "1CFC08"
    assert decoded["identity.cell_manufacturer_code"].value == 0x1C
    assert decoded["battery.installed_capacity"].value == 230.0


def test_110_decodes_fault_tables_and_mos_state():
    decoded = values(frame("110", "0100000000008031"))
    assert decoded["pack.active_v33_conditions"].value == [
        "cell_over_voltage",
        "charge_voltage_low",
    ]
    assert decoded["pack.any_v33_condition_active"].value is True
    assert decoded["mos.parallel_complete"].value is True
    assert decoded["mos.charge_closed"].value is True
    assert decoded["mos.discharge_closed"].value is True
    assert decoded["mos.precharge_closed"].value is False


def test_v33_extended_measurement_and_status_frames():
    decoded_250 = values(frame("250", "220170FED300E600"))
    assert decoded_250["pack.maximum_mos_temperature"].value == 29.0
    assert decoded_250["pack.heating_film_temperature"].value == -40.0
    assert decoded_250["pack.maximum_allowable_charge_current"].value == 211.0
    assert decoded_250["pack.maximum_allowable_discharge_current"].value == 230.0

    decoded_364 = values(frame("364", "0101000001000000"))
    assert decoded_364["modules.normal"].value == 1
    assert decoded_364["modules.charge_disabled"].value == 1
    assert decoded_364["modules.parallel_connected"].value == 1

    decoded_371 = values(frame("371", "4408FC0800000000"))
    assert decoded_371["limits.array_max_charge_current"].value == 211.6
    assert decoded_371["limits.array_max_discharge_current"].value == 230.0


def test_400_decodes_mode_fault_balance_and_unnamed_substate():
    decoded = values(frame("400", "0200070009000800"))
    assert decoded["system.operation_mode"].value == "discharge"
    assert decoded["system.fault_level"].value == "none"
    assert decoded["battery.cycle_count"].value == 7
    assert decoded["cells.balance_mask"].value == 9
    assert decoded["cells.balancing"].value == [1, 4]
    assert decoded["system.substate_raw"].value == 0x0008


def test_versions_energy_serial_and_history_frames():
    decoded_363 = values(frame("363", "F002F00200000000"))
    assert decoded_363["identity.host_software_version_raw"].value == 0x02F0
    assert decoded_363["identity.host_hardware_version_raw"].value == 0x02F0

    decoded_500 = values(frame("500", "F002AA56312E3046"))
    assert decoded_500["identity.boot_version_marker_valid"].value is True
    assert decoded_500["identity.boot_version"].value == "V1.0F"

    decoded_550 = values(frame("550", "2E18000035020000"))
    assert decoded_550["history.charged_energy"].value == 6.19
    assert decoded_550["history.discharged_energy"].value == 0.565

    assert values(frame("600", "4142434445464748"))["identity.serial_first_half"].value == "ABCDEFGH"
    assert values(frame("650", "3132333435363738"))["identity.serial_second_half"].value == "12345678"

    decoded_700 = values(frame("700", "02001A0000000000"))
    assert decoded_700["history.overcharge_count"].value == 2
    assert decoded_700["history.overdischarge_count"].value == 26

    decoded_750 = values(frame("750", "0100020003000400"))
    assert decoded_750["history.charge_overcurrent_count"].value == 1
    assert decoded_750["history.discharge_over_temperature_count"].value == 4


def test_short_ccl_pulse_is_preserved_as_event_without_control_action():
    cache = VirtualBatteryCache()
    cache.apply(frame("356", "C8140000E6000000", 1000.0))
    cache.apply(frame("110", "0000000000000021", 1000.1))
    cache.apply(frame("351", "48020000FC08E001", 1000.2))
    cache.apply(frame("351", "48024408FC08E001", 1001.2))
    cache.apply(frame("351", "48020000FC08E001", 1002.26))
    snapshot = cache.snapshot(at=1002.26)
    events = snapshot["anomaly_events"]
    assert any(event["event"] == "positive_ccl_with_charge_mos_open" for event in events)
    pulse = next(event for event in events if event["event"] == "ccl_pulse_without_measured_charge")
    assert pulse["details"]["peak_charge_current_limit_a"] == 211.6
    assert pulse["details"]["duration_seconds"] == 1.06
    assert pulse["details"]["raw_deye_current_a"] == 0.0
    assert pulse["action_taken"] is False


def test_cache_expires_each_critical_field_but_retains_session_and_config():
    cache = VirtualBatteryCache()
    cache.apply(frame("356", "E6140000E6000000", 1000.0))
    cache.apply(frame("35E", "44593030311CFC08", 1000.0))
    snapshot = cache.snapshot(at=1006.0)
    assert snapshot["fields"]["battery.voltage"]["cached_value"] == 53.5
    assert snapshot["fields"]["battery.voltage"]["effective_value"] is None
    assert snapshot["fields"]["battery.voltage"]["freshness"] == "stale"
    assert snapshot["fields"]["identity.manufacturer"]["effective_value"] == "DY"
    assert snapshot["fields"]["identity.pack_number"]["effective_value"] == "001"
    assert snapshot["fields"]["battery.installed_capacity"]["effective_value"] == 230.0


def test_invalid_update_does_not_refresh_last_valid_value():
    cache = VirtualBatteryCache()
    cache.apply(frame("355", "6400640000000000", 1000.0))
    cache.apply(frame("355", "6500640000000000", 1004.0))
    soc = cache.snapshot(at=1004.0)["fields"]["battery.soc"]
    assert soc["cached_value"] == 100.0
    assert soc["last_valid_timestamp"] == 1000.0
    assert soc["rejected_updates"] == 1
    assert soc["last_rejected_value"] == 101.0


def test_field_with_only_invalid_observation_is_not_usable():
    cache = VirtualBatteryCache()
    cache.apply(frame("355", "6500640000000000", 1000.0))
    soc = cache.snapshot(at=1000.0)["fields"]["battery.soc"]
    assert soc["cached_value"] is None
    assert soc["effective_value"] is None
    assert soc["freshness"] == "invalid"
    assert soc["usable"] is False


def test_voltage_crosscheck_is_diagnostic_only():
    cache = VirtualBatteryCache()
    cache.apply(frame("356", "E6140000E6000000", 1000.0))
    cache.apply(frame("150", "17020000E403E803", 1000.1))
    health = cache.snapshot(at=1000.1)["health"]
    assert health["voltage_356_vs_150_delta_v"] == 0.0
    assert health["voltage_crosscheck_over_1v"] is False
    assert health["control_ready"] is False


def test_missing_stale_and_reported_zero_soc_remain_distinct():
    empty = VirtualBatteryCache().snapshot(at=1000.0)
    assert empty["health"]["soc_observation"] == "missing"

    cache = VirtualBatteryCache()
    cache.apply(frame("356", "E6140000E6000000", 1000.0))
    cache.apply(frame("355", "6400640000000000", 1000.0))
    assert cache.snapshot(at=1006.0)["health"]["soc_observation"] == "stale"

    cache.apply(frame("356", "E6140000E6000000", 1007.0))
    cache.apply(frame("355", "0000640000000000", 1007.0))
    snapshot = cache.snapshot(at=1007.0)
    assert snapshot["health"]["soc_observation"] == "reported_zero"
    names = [event["event"] for event in snapshot["anomaly_events"]]
    assert "soc_zero_at_high_voltage" in names
    assert "implausibly_fast_soc_drop" in names
    assert all(event["action_taken"] is False for event in snapshot["anomaly_events"])


def test_stream_timing_tracks_keepalive_and_battery_gaps_separately():
    cache = VirtualBatteryCache()
    cache.apply(frame("305", "0000000000000000", 1000.0, "T"))
    cache.apply(frame("305", "0000000000000000", 1001.2, "T"))
    cache.apply(frame("355", "6400640000000000", 1000.1))
    cache.apply(frame("355", "6400640000000000", 1002.6))
    timing = cache.snapshot(at=1002.6)["statistics"]["maximum_interframe_gap_seconds"]
    assert timing["0x305:T"] == 1.2
    assert timing["0x355:R"] == 2.5
