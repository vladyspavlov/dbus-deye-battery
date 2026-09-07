from pathlib import Path

import pytest

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import CandumpParseError, parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.stateful import StatefulShadow
from tools.validate_real_captures import validate


DATA = Path(__file__).resolve().parent / "data"
PRESERVED = DATA / "deye-se-f12c-sample-5min.log"

# Longer recordings are not distributed: they are hours of one installation's
# telemetry.  Drop your own candump logs into tests/data/private/ with these
# names to exercise the extended checks; without them the tests skip.
PRIVATE = DATA / "private"
CHARGE_RETRY = PRIVATE / "charge-retry.log"
HIGH_CELL_AND_GAP = PRIVATE / "high-cell-and-gap.log"


def frames_between(path: Path, start: float, end: float):
    with path.open(encoding="ascii", errors="replace") as stream:
        for line in stream:
            try:
                frame = parse_candump_line(line)
            except CandumpParseError:
                continue
            if start <= frame.timestamp <= end:
                yield frame


def test_validator_passes_preserved_real_capture():
    result = validate([PRESERVED])

    assert result["result"] == "pass"
    assert result["statistics"]["total_frames"] == 5465
    assert result["statistics"]["decode_errors"] == 0
    assert result["observations"]["nominal_capacities_ah"] == [230.0]
    assert result["coverage"]["real_active_alarm_or_warning_bits_observed"] is False


@pytest.mark.skipif(not CHARGE_RETRY.exists(), reason="optional private capture not installed in tests/data/private")
def test_real_one_second_ccl_pulse_cannot_raise_effective_charge_limit():
    cache = VirtualBatteryCache()
    pulse = None
    for frame in frames_between(CHARGE_RETRY, 1788097300.0, 1788097356.0):
        cache.apply(frame)
        if frame.can_id == 0x351 and frame.payload == bytes.fromhex("48024408FC08E001"):
            pulse = build_mock_dbus_model(cache.snapshot(at=frame.timestamp))
            break

    assert pulse is not None
    assert pulse["policy"]["raw_limits"]["ccl_a"] == 211.6
    assert pulse["policy"]["raw_limits"]["array_ccl_a"] == 0.0
    assert pulse["policy"]["diagnostics"]["charge_mos_closed"] is False
    assert pulse["paths"]["/Info/MaxChargeCurrent"] == 0.0
    assert pulse["paths"]["/Info/MaxChargeVoltage"] == 55.2
    assert pulse["paths"]["/Info/MaxDischargeCurrent"] == 230.0


@pytest.mark.skipif(not HIGH_CELL_AND_GAP.exists(), reason="optional private capture not installed in tests/data/private")
def test_real_high_cell_event_raises_alarm_without_stopping_discharge():
    cache = VirtualBatteryCache()
    event = None
    for frame in frames_between(HIGH_CELL_AND_GAP, 1788179700.0, 1788179780.0):
        cache.apply(frame)
        if frame.can_id not in {0x200, 0x361}:
            continue
        model = build_mock_dbus_model(cache.snapshot(at=frame.timestamp))
        if model["paths"].get("/Alarms/HighCellVoltage") == 2:
            event = model
            break

    assert event is not None
    assert event["paths"]["/Info/MaxChargeCurrent"] == 0.0
    assert event["paths"]["/Alarms/HighCellVoltage"] == 2
    assert event["paths"]["/Info/MaxDischargeCurrent"] == 230.0
    assert event["paths"]["/Io/AllowToDischarge"] == 1
    assert event["policy"]["permissions"]["would_directly_switch_inverter_off"] is False


@pytest.mark.skipif(not HIGH_CELL_AND_GAP.exists(), reason="optional private capture not installed in tests/data/private")
def test_real_recorder_gap_exercises_loss_and_reconnect_lifecycle():
    before_gap = list(frames_between(HIGH_CELL_AND_GAP, 1788183828.0, 1788183837.3))
    after_gap = list(frames_between(HIGH_CELL_AND_GAP, 1788187426.7, 1788187432.0))
    assert before_gap and after_gap

    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    model = None
    for frame in before_gap:
        cache.apply(frame)
        model = shadow.step(cache.snapshot(at=frame.timestamp), timestamp=frame.timestamp)
    assert model is not None
    assert model["lifecycle"]["state"] == "online"

    loss_time = before_gap[-1].timestamp + 3.25
    lost = shadow.step(cache.snapshot(at=loss_time), timestamp=loss_time)
    assert lost["lifecycle"]["state"] == "can_lost"
    assert lost["paths"]["/Connected"] == 0
    assert lost["paths"]["/Info/MaxDischargeCurrent"] is None
    assert lost["paths"]["/Alarms/HighDischargeCurrent"] is None

    states = []
    for frame in after_gap:
        cache.apply(frame)
        model = shadow.step(cache.snapshot(at=frame.timestamp), timestamp=frame.timestamp)
        states.append(model["lifecycle"]["state"])
    assert "reconnecting" in states
    assert states[-1] == "online"
