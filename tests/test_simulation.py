import json

from deye_virtual_battery.simulation import (
    simulate_recordings,
    simulate_stateful_recordings,
)


def test_recording_simulation_keeps_vebus_only_spike_out_of_standard_alarm(tmp_path):
    can_path = tmp_path / "can.log"
    can_path.write_text(
        "(1000.000000) can0 110#0000000000000021 R\n"
        "(1000.010000) can0 200#2D0D080DE600E600 R\n"
        "(1000.020000) can0 351#48020000FC08E001 R\n"
        "(1000.030000) can0 355#6400640000000000 R\n"
        "(1000.040000) can0 356#D2140000E6000000 R\n"
        "(1000.050000) can0 359#0000000000000000 R\n"
        "(1000.060000) can0 361#2D0D080DE600E600 R\n"
        "(1000.070000) can0 371#0000FC0800000000 R\n"
    )
    vebus_path = tmp_path / "vebus.jsonl"
    records = [
        {"record_type": "value", "timestamp": 1000.5, "path": "/Dc/0/Voltage", "value": 53.3},
        {"record_type": "value", "timestamp": 1000.8, "path": "/Dc/0/Voltage", "value": 60.78},
    ]
    vebus_path.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = simulate_recordings([can_path], [vebus_path])
    event = result["most_severe_mismatch"]
    assert result["action_taken"] is False
    assert result["remote_contacted"] is False
    assert result["maximum_vebus_voltage_v"] == 60.78
    assert event["vebus_minus_deye_v"] == 7.48
    assert event["integration_alarm_level"] == 2
    assert event["effective_ccl_a"] == 0.0
    assert event["effective_dcl_a"] == 230.0
    assert event["would_request_discharge_stop"] is False
    assert event["would_directly_switch_inverter_off"] is False
    assert event["standard_high_voltage_alarm"] == 0


def test_recording_simulation_reorders_small_capture_timestamp_inversions(tmp_path):
    can_path = tmp_path / "can.log"
    can_path.write_text(
        "(1000.500000) can0 351#48020000FC08E001 R\n"
        "(1000.200000) can0 356#D2140000E6000000 R\n"
        "(1000.600000) can0 355#6400640000000000 R\n"
    )
    vebus_path = tmp_path / "vebus.jsonl"
    vebus_path.write_text(
        json.dumps(
            {
                "record_type": "value",
                "timestamp": 1000.7,
                "path": "/Dc/0/Voltage",
                "value": 53.3,
            }
        )
        + "\n"
    )
    result = simulate_recordings([can_path], [vebus_path])
    assert result["can_statistics"]["total_frames"] == 3


def test_stateful_recording_simulation_never_selects_bms_in_current_scenario(tmp_path):
    can_path = tmp_path / "can.log"
    lines = []
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
    for second in range(5):
        for index, (can_id, payload) in enumerate(payloads):
            lines.append(
                f"({1000 + second + index * 0.01:.6f}) can0 {can_id}#{payload} R\n"
            )
    can_path.write_text("".join(lines))
    vebus_path = tmp_path / "vebus.jsonl"
    records = [
        {
            "record_type": "value",
            "timestamp": 1000.2 + index * 0.25,
            "path": "/Dc/0/Voltage",
            "value": 60.78,
        }
        for index in range(17)
    ]
    vebus_path.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = simulate_stateful_recordings([can_path], [vebus_path])
    voltage_events = [
        event
        for event in result["alarm_events"]
        if event["path"] == "/Diagnostics/Alarms/VoltageDisagreement"
    ]
    assert result["action_taken"] is False
    assert result["remote_contacted"] is False
    assert result["registered_on_dbus"] is False
    assert result["current_no_bms_bms_lost_samples"] == 0
    assert result["final_current_no_bms_systemcalc"]["control_bms_parameters"] == 0
    assert len(voltage_events) == 1
