from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.stateful import StatefulShadow
from deye_virtual_battery.venus_simulator import (
    CURRENT_NO_BMS_SCENARIO,
    HYPOTHETICAL_VIRTUAL_BMS_SCENARIO,
    simulate_systemcalc,
)


def frame(can_id: str, payload: str, timestamp: float):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} R")


def add_complete_cycle(cache: VirtualBatteryCache, timestamp: float) -> None:
    payloads = (
        ("110", "0000000000000021"),
        ("200", "2D0D080DE600E600"),
        ("351", "48020000FC08E001"),
        ("355", "6400640000000000"),
        ("356", "D2140000E6000000"),
        ("359", "0000000000000000"),
        ("361", "2D0D080DE600E600"),
        ("371", "0000FC0800000000"),
    )
    for index, (can_id, payload) in enumerate(payloads):
        cache.apply(frame(can_id, payload, timestamp + index * 0.01))


def step_with_cycle(
    shadow: StatefulShadow,
    cache: VirtualBatteryCache,
    timestamp: float,
    *,
    vebus_voltage_v: float = 53.3,
):
    add_complete_cycle(cache, timestamp - 0.1)
    return shadow.step(
        cache.snapshot(at=timestamp),
        timestamp=timestamp,
        vebus_voltage_v=vebus_voltage_v,
    )


def bring_online(shadow: StatefulShadow, cache: VirtualBatteryCache, start: float = 1000.0):
    first = step_with_cycle(shadow, cache, start)
    assert first["lifecycle"]["state"] == "starting"
    return step_with_cycle(shadow, cache, start + 2.1)


def test_startup_can_loss_and_qualified_reconnect_are_data_only():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    online = bring_online(shadow, cache)
    assert online["lifecycle"]["state"] == "online"
    assert online["paths"]["/Connected"] == 1
    assert online["would_register_mock_service"] is True
    assert online["registered_on_dbus"] is False
    assert online["action_taken"] is False

    lost_at = 1007.2
    lost = shadow.step(cache.snapshot(at=lost_at), timestamp=lost_at)
    assert lost["lifecycle"]["state"] == "can_lost"
    assert lost["paths"]["/Connected"] == 0
    assert lost["paths"]["/Dc/0/Voltage"] is None
    assert lost["paths"]["/Info/MaxDischargeCurrent"] is None
    assert lost["would_register_mock_service"] is True
    assert lost["paths"]["/Diagnostics/Lifecycle/VebusBolLossWindowActive"] == 1
    assert lost["paths"]["/Diagnostics/Lifecycle/VebusBolTimeoutRemaining"] == 300.0
    assert lost["paths"]["/Diagnostics/Lifecycle/VebusBolTimeoutExpected"] == 0

    reconnecting = step_with_cycle(shadow, cache, 1008.0)
    assert reconnecting["lifecycle"]["state"] == "reconnecting"
    assert reconnecting["paths"]["/Connected"] == 0
    still_reconnecting = step_with_cycle(shadow, cache, 1010.9)
    assert still_reconnecting["lifecycle"]["state"] == "reconnecting"
    reconnected = step_with_cycle(shadow, cache, 1011.1)
    assert reconnected["lifecycle"]["state"] == "online"
    assert reconnected["paths"]["/Connected"] == 1
    assert reconnected["paths"][
        "/Diagnostics/Lifecycle/VebusBolLossWindowActive"
    ] == 0
    assert reconnected["paths"][
        "/Diagnostics/Lifecycle/VebusBolTimeoutRemaining"
    ] is None


def test_continuing_can_with_missing_critical_frames_is_distinct_from_can_loss():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    bring_online(shadow, cache, 1100.0)
    # Only an optional identity frame continues; the bus is alive while the
    # control fields have exceeded their independent three-second timeout.
    cache.apply(frame("35E", "44593030311CFC08", 1107.0))
    stale = shadow.step(cache.snapshot(at=1107.1), timestamp=1107.1)
    assert stale["lifecycle"]["state"] == "critical_data_stale"
    assert stale["lifecycle"]["can_alive"] is True
    assert stale["lifecycle"]["loss_reason"].startswith("critical_fields_stale:")


def test_warning_raise_is_debounced_and_notifications_are_edge_triggered():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    bring_online(shadow, cache, 2000.0)

    candidate = step_with_cycle(shadow, cache, 2002.2, vebus_voltage_v=55.0)
    assert candidate["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 0
    raised = step_with_cycle(shadow, cache, 2004.3, vebus_voltage_v=55.0)
    assert raised["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 1
    assert len([event for event in raised["events"] if event["event"] == "alarm_transition"]) == 1

    for timestamp in (2004.5, 2004.7, 2004.9, 2005.1):
        held = step_with_cycle(shadow, cache, timestamp, vebus_voltage_v=55.0)
        assert not [event for event in held["events"] if event["event"] == "alarm_transition"]
    voltage_events = [
        event
        for event in shadow.transitions
        if event.get("path") == "/Diagnostics/Alarms/VoltageDisagreement"
    ]
    assert len(voltage_events) == 1


def test_voltage_alarm_uses_amplitude_and_time_hysteresis_before_clear():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    bring_online(shadow, cache, 3000.0)
    step_with_cycle(shadow, cache, 3002.2, vebus_voltage_v=60.78)
    step_with_cycle(shadow, cache, 3002.8, vebus_voltage_v=60.78)

    # Delta 4.5 V is below the 5 V entry threshold but above the 4 V
    # level-2 exit threshold, so the alarm remains level 2.
    held = step_with_cycle(shadow, cache, 3003.0, vebus_voltage_v=57.8)
    assert held["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2

    clear_candidate = step_with_cycle(shadow, cache, 3003.2, vebus_voltage_v=53.3)
    assert clear_candidate["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    almost = step_with_cycle(shadow, cache, 3013.1, vebus_voltage_v=53.3)
    assert almost["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    cleared = step_with_cycle(shadow, cache, 3013.3, vebus_voltage_v=53.3)
    assert cleared["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 0


def test_warning_escalates_immediately_to_level_two_voltage_alarm():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    bring_online(shadow, cache, 3500.0)
    step_with_cycle(shadow, cache, 3502.2, vebus_voltage_v=55.0)
    warning = step_with_cycle(shadow, cache, 3504.3, vebus_voltage_v=55.0)
    assert warning["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 1
    alarm = step_with_cycle(shadow, cache, 3504.4, vebus_voltage_v=60.78)
    assert alarm["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    assert any(
        event.get("previous_level") == 1 and event.get("new_level") == 2
        for event in alarm["events"]
    )


def test_level_two_voltage_spike_raises_immediately_but_does_not_stop_discharge():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    bring_online(shadow, cache, 4000.0)
    raised = step_with_cycle(shadow, cache, 4002.2, vebus_voltage_v=60.78)
    assert raised["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    assert raised["instantaneous_policy"]["permissions"]["allow_discharge"] is True
    assert raised["instantaneous_policy"]["permissions"]["would_directly_switch_inverter_off"] is False
    recovered = step_with_cycle(shadow, cache, 4002.5, vebus_voltage_v=53.3)
    assert recovered["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    assert len([
        event
        for event in shadow.transitions
        if event.get("path") == "/Diagnostics/Alarms/VoltageDisagreement"
    ]) == 1


def test_subsecond_warning_level_mismatch_is_debounced():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    bring_online(shadow, cache, 4500.0)
    candidate = step_with_cycle(shadow, cache, 4502.2, vebus_voltage_v=54.5)
    assert candidate["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 0
    recovered = step_with_cycle(shadow, cache, 4502.5, vebus_voltage_v=53.3)
    assert recovered["stateful_alarms"]["/Diagnostics/Alarms/VoltageDisagreement"] == 0
    assert not [
        event
        for event in shadow.transitions
        if event.get("path") == "/Diagnostics/Alarms/VoltageDisagreement"
    ]


def test_mock_systemcalc_separates_current_no_bms_and_selected_bms_loss():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    online = bring_online(shadow, cache, 5000.0)
    current = simulate_systemcalc(online, scenario=CURRENT_NO_BMS_SCENARIO)
    selected = simulate_systemcalc(
        online, scenario=HYPOTHETICAL_VIRTUAL_BMS_SCENARIO
    )
    assert current["active_bms_service"] is None
    assert current["control_bms_parameters"] == 0
    assert selected["active_bms_service"] == "com.victronenergy.battery.deye_se_f12"
    assert selected["control_bms_parameters"] == 1
    assert selected["dvcc_inputs"]["max_discharge_current_a"] == 230.0

    lost_at = 5007.2
    lost = shadow.step(cache.snapshot(at=lost_at), timestamp=lost_at)
    current_lost = simulate_systemcalc(lost, scenario=CURRENT_NO_BMS_SCENARIO)
    selected_lost = simulate_systemcalc(
        lost, scenario=HYPOTHETICAL_VIRTUAL_BMS_SCENARIO
    )
    assert current_lost["bms_connection_lost_alarm"] == 0
    assert selected_lost["bms_connection_lost_alarm"] == 0
    assert selected_lost["control_bms_parameters"] == 0
    assert selected_lost["risk_observations"]["bms_loss_shutdown_pending"] is True
    assert selected_lost["risk_observations"][
        "vebus_bol_timeout_remaining_seconds"
    ] == 300.0
    assert selected_lost["risk_observations"]["ac_continuity_not_guaranteed_if_selected_bms_is_lost"] is True
    assert selected_lost["writes_vebus_mode"] is False

    timed_out = shadow.step(cache.snapshot(at=5307.3), timestamp=5307.3)
    selected_timed_out = simulate_systemcalc(
        timed_out, scenario=HYPOTHETICAL_VIRTUAL_BMS_SCENARIO
    )
    assert selected_timed_out["bms_connection_lost_alarm"] == 1
    assert selected_timed_out["risk_observations"]["bms_loss_timeout_elapsed"] is True
    assert selected_timed_out["risk_observations"][
        "vebus_bol_timeout_remaining_seconds"
    ] == 0.0
