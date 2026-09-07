from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.decoder import FAULT_TABLE_BITS
from deye_virtual_battery.policy import evaluate_policy


def frame(can_id: str, payload: str, timestamp: float = 1000.0, direction: str = "R"):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} {direction}")


def baseline_cache(*, charging_allowed: bool = False) -> VirtualBatteryCache:
    cache = VirtualBatteryCache()
    if charging_allowed:
        frame_110 = "0000000000000031"
        frame_351 = "48024408FC08E001"
        frame_371 = "4408FC0800000000"
    else:
        frame_110 = "0000000000000021"
        frame_351 = "48020000FC08E001"
        frame_371 = "0000FC0800000000"
    frames = (
        frame("110", frame_110, 1000.00),
        frame("200", "2D0D080DE600E600", 1000.01),
        frame("351", frame_351, 1000.02),
        frame("355", "6400640000000000", 1000.03),
        frame("356", "E6140000E6000000", 1000.04),
        frame("359", "0000000000000000", 1000.05),
        frame("35C", "4000000000000000", 1000.06),
        frame("35E", "44593030311CFC08", 1000.07),
        frame("361", "2D0D080DE600E600", 1000.08),
        frame("364", "0101000001000000", 1000.09),
        frame("371", frame_371, 1000.10),
        frame("400", "0000000000001200", 1000.11),
    )
    cache.apply_all(frames)
    return cache


def condition_payload(table: int, bit: int, *, mos_flags: int = 0x31) -> str:
    payload = bytearray(8)
    payload[table - 1] = 1 << bit
    payload[7] = mos_flags
    return payload.hex()


def test_blocked_charge_gets_zero_ccl_and_reduced_provisional_cvl():
    policy = evaluate_policy(baseline_cache().snapshot(at=1000.2))
    assert policy["critical_data_ready"] is True
    assert policy["raw_limits"]["cvl_v"] == 58.4
    assert policy["effective_limits"]["ccl_a"] == 0.0
    assert policy["effective_limits"]["cvl_v"] == 55.2
    assert policy["effective_limits"]["dcl_a"] == 230.0
    assert policy["permissions"]["allow_charge"] is False
    assert policy["permissions"]["allow_discharge"] is True
    assert policy["permissions"]["would_directly_switch_inverter_off"] is False
    assert all(
        value in (0, None) for value in policy["standard_alarms"].values()
    )
    assert policy["standard_alarms"]["/Alarms/LowSoc"] is None
    assert policy["production_control_allowed"] is False


def test_positive_ccl_and_closed_charge_mos_allow_charge_despite_summary_conflicts():
    policy = evaluate_policy(baseline_cache(charging_allowed=True).snapshot(at=1000.2))
    assert policy["effective_limits"]["ccl_a"] == 211.6
    assert policy["effective_limits"]["cvl_v"] == 57.2
    assert policy["permissions"]["allow_charge"] is True
    # Live 0x35C and 0x364 summaries remained contradictory during charge pulses;
    # neither is allowed to veto the cross-checked CCL + MOS result alone.
    assert "vebus_battery_voltage_disagreement" not in policy["permissions"][
        "charge_inhibit_reasons"
    ]


def test_charge_alarm_is_telemetry_only_when_limits_and_mos_still_allow_charge():
    cache = baseline_cache(charging_allowed=True)
    cache.apply(frame("110", "0100000000000031", 1001.0))
    policy = evaluate_policy(cache.snapshot(at=1001.0))
    assert policy["standard_alarms"]["/Alarms/HighCellVoltage"] == 2
    assert policy["effective_limits"]["ccl_a"] == 211.6
    assert policy["effective_limits"]["dcl_a"] == 230.0
    assert policy["permissions"]["would_request_discharge_stop"] is False
    assert policy["diagnostics"]["alarm_flags_affect_directional_limits"] is False


def test_discharge_alarm_is_telemetry_only_when_limits_and_mos_still_allow_discharge():
    cache = baseline_cache(charging_allowed=True)
    cache.apply(frame("110", "0200000000000031", 1001.0))
    policy = evaluate_policy(cache.snapshot(at=1001.0))
    assert policy["standard_alarms"]["/Alarms/LowCellVoltage"] == 2
    assert policy["effective_limits"]["dcl_a"] == 230.0
    assert policy["permissions"]["may_stop_inverter_when_islanded"] is False
    assert policy["permissions"]["would_directly_switch_inverter_off"] is False
    assert policy["writes_vebus_mode"] is False


def test_open_discharge_mos_requests_dcl_zero_independently_of_alarm_flags():
    cache = baseline_cache(charging_allowed=True)
    cache.apply(frame("110", "0000000000000011", 1001.0))
    policy = evaluate_policy(cache.snapshot(at=1001.0))
    assert max(value or 0 for value in policy["standard_alarms"].values()) == 0
    assert policy["effective_limits"]["dcl_a"] == 0.0
    assert policy["permissions"]["may_stop_inverter_when_islanded"] is True
    assert "discharge_mos_open" in policy["permissions"]["discharge_inhibit_reasons"]


def test_warning_maps_to_level_one_without_stopping_discharge():
    cache = baseline_cache(charging_allowed=True)
    cache.apply(frame("110", "0000000001000031", 1001.0))
    policy = evaluate_policy(cache.snapshot(at=1001.0))
    assert policy["standard_alarms"]["/Alarms/HighCellVoltage"] == 1
    assert policy["effective_limits"]["dcl_a"] == 230.0
    assert policy["permissions"]["would_request_discharge_stop"] is False


def test_every_v33_warning_is_visible_but_never_changes_directional_limits():
    checked = 0
    for table in (5, 6):
        for bit, condition in enumerate(FAULT_TABLE_BITS[table]):
            cache = baseline_cache(charging_allowed=True)
            cache.apply(frame("110", condition_payload(table, bit), 1001.0))
            policy = evaluate_policy(cache.snapshot(at=1001.0))

            assert policy["active_deye_conditions"] == [condition]
            assert policy["effective_limits"]["ccl_a"] == 211.6
            assert policy["effective_limits"]["dcl_a"] == 230.0
            levels = [
                value
                for value in (
                    *policy["standard_alarms"].values(),
                    *policy["custom_alarms"].values(),
                )
                if isinstance(value, int)
            ]
            assert max(levels) == 1
            assert policy["permissions"]["would_directly_switch_inverter_off"] is False
            checked += 1

    assert checked == 16


def test_every_v33_protection_is_level_two_without_direct_mode_off():
    checked = 0
    for table in (1, 2, 3, 4, 7):
        for bit, condition in enumerate(FAULT_TABLE_BITS[table]):
            cache = baseline_cache(charging_allowed=True)
            cache.apply(frame("110", condition_payload(table, bit), 1001.0))
            policy = evaluate_policy(cache.snapshot(at=1001.0))

            assert policy["active_deye_conditions"] == [condition]
            levels = [
                value
                for value in (
                    *policy["standard_alarms"].values(),
                    *policy["custom_alarms"].values(),
                )
                if isinstance(value, int)
            ]
            assert max(levels) == 2
            assert policy["effective_limits"]["ccl_a"] == 211.6
            assert policy["effective_limits"]["dcl_a"] == 230.0
            assert policy["permissions"]["would_directly_switch_inverter_off"] is False
            assert policy["writes_vebus_mode"] is False
            checked += 1

    assert checked == 40


def test_unmapped_protection_keeps_level_two_severity_without_stopping_discharge():
    cache = baseline_cache(charging_allowed=True)
    cache.apply(frame("110", condition_payload(2, 3), 1001.0))
    policy = evaluate_policy(cache.snapshot(at=1001.0))

    assert policy["active_deye_conditions"] == ["cell_temperature_difference_high"]
    assert policy["custom_alarms"]["/Diagnostics/Alarms/UnmappedDeyeCondition"] == 2
    assert policy["custom_alarms"][
        "/Diagnostics/Alarms/ProtectionWithoutDirectionalLimit"
    ] == 2
    assert policy["effective_limits"]["ccl_a"] == 211.6
    assert policy["effective_limits"]["dcl_a"] == 230.0


def test_system_fault_level_has_visible_severity_but_does_not_change_limits():
    minor_cache = baseline_cache(charging_allowed=True)
    minor_cache.apply(frame("400", "0001000000001200", 1001.0))
    minor = evaluate_policy(minor_cache.snapshot(at=1001.0))
    assert minor["active_deye_conditions"] == ["system_minor_fault"]
    assert minor["standard_alarms"]["/Alarms/InternalFailure"] == 1
    assert minor["effective_limits"]["ccl_a"] == 211.6
    assert minor["effective_limits"]["dcl_a"] == 230.0

    major_cache = baseline_cache(charging_allowed=True)
    major_cache.apply(frame("400", "0002000000001200", 1001.0))
    major = evaluate_policy(major_cache.snapshot(at=1001.0))
    assert major["active_deye_conditions"] == ["system_major_fault"]
    assert major["standard_alarms"]["/Alarms/InternalFailure"] == 2
    assert major["effective_limits"]["ccl_a"] == 211.6
    assert major["effective_limits"]["dcl_a"] == 230.0
    assert major["permissions"]["would_directly_switch_inverter_off"] is False


def test_vebus_only_60v_is_custom_alarm_and_charge_inhibit_not_shutdown():
    policy = evaluate_policy(
        baseline_cache().snapshot(at=1000.2),
        vebus_voltage_v=60.78,
    )
    assert policy["voltage_observations"]["vebus_minus_deye_v"] == 7.28
    assert policy["custom_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    assert policy["standard_alarms"]["/Alarms/HighVoltage"] == 0
    assert policy["effective_limits"]["ccl_a"] == 0.0
    assert policy["effective_limits"]["dcl_a"] == 230.0
    assert policy["permissions"]["would_request_discharge_stop"] is False
    assert policy["permissions"]["would_directly_switch_inverter_off"] is False


def test_stale_critical_data_prevents_mock_service_publication():
    snapshot = baseline_cache().snapshot(at=1007.0)
    model = build_mock_dbus_model(snapshot)
    assert model["registered_on_dbus"] is False
    assert model["paths"]["/Connected"] == 0
    assert model["paths"]["/Info/MaxChargeCurrent"] is None
    assert model["paths"]["/Info/MaxDischargeCurrent"] is None
    assert model["policy"]["custom_alarms"]["/Diagnostics/Alarms/CriticalDataStale"] == 2


def test_mock_dbus_identity_is_deye_and_never_lg_or_pylon():
    model = build_mock_dbus_model(baseline_cache().snapshot(at=1000.2))
    assert model["service_name"] == "com.victronenergy.battery.deye_se_f12"
    assert model["paths"]["/Manufacturer"] == "Deye"
    assert model["paths"]["/ProductName"] == "Deye SE-F12-C"
    assert model["paths"]["/ProductId"] not in {0xB004, 0xB009}
    assert model["paths"]["/Diagnostics/Policy/WritesVebusMode"] == 0


def test_mock_dbus_publishes_online_capacity_and_keeps_cell_ids_invalid():
    cache = baseline_cache()
    cache.apply(frame("355", "6000640000000000", 1001.0))
    model = build_mock_dbus_model(cache.snapshot(at=1001.0))
    paths = model["paths"]

    assert paths["/InstalledCapacity"] == 230.0
    assert paths["/Soc"] == 96.0
    assert paths["/Capacity"] == 230.0
    assert paths["/ConsumedAmphours"] == 9.2
    assert paths["/Diagnostics/Capacity/Derived"] == 1
    assert paths["/System/NrOfModulesOnline"] == 1
    assert paths["/System/NrOfModulesBlockingCharge"] == 1
    assert paths["/System/NrOfModulesBlockingDischarge"] == 0
    assert paths["/System/NrOfModulesOffline"] == 0
    assert paths["/System/MaxCellTemperature"] == 23.0
    assert paths["/System/MinCellTemperature"] == 23.0
    assert paths["/System/MaxVoltageCellId"] is None
    assert paths["/System/MinVoltageCellId"] is None
    assert paths["/System/MaxTemperatureCellId"] is None
    assert paths["/System/MinTemperatureCellId"] is None
    assert paths["/Diagnostics/Cells/ExtremaIdsAvailable"] == 0


def test_mock_dbus_does_not_publish_stale_derived_capacity_as_zero():
    paths = build_mock_dbus_model(baseline_cache().snapshot(at=1007.0))["paths"]
    assert paths["/InstalledCapacity"] == 230.0
    assert paths["/Soc"] is None
    assert paths["/Capacity"] is None
    assert paths["/ConsumedAmphours"] is None
