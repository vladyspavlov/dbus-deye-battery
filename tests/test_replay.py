from pathlib import Path

from deye_virtual_battery.cli import replay
from deye_virtual_battery.dbus_model import build_mock_dbus_model


DATA = Path(__file__).resolve().parent / "data"
# A redacted five-minute recording from a live SE-F12-C on the Sol-ark
# profile.  The two serial-number frames are replaced with placeholders.
CAPTURE = DATA / "deye-se-f12c-sample-5min.log"


def test_preserved_five_minute_capture_replays_to_expected_shadow_state():
    snapshot = replay([CAPTURE], 230.0).snapshot()
    fields = snapshot["fields"]
    assert snapshot["statistics"]["total_frames"] == 5465
    assert snapshot["statistics"]["decode_errors"] == 0
    assert snapshot["statistics"]["unknown_or_wrong_direction_frames"] == 0
    assert fields["battery.voltage"]["effective_value"] == 53.5
    assert fields["battery.current"]["effective_value"] == 0.0
    assert fields["battery.soc"]["effective_value"] == 100.0
    assert fields["limits.max_charge_voltage"]["effective_value"] == 58.4
    assert fields["limits.max_charge_current"]["effective_value"] == 0.0
    assert fields["identity.manufacturer"]["effective_value"] == "DY"
    assert fields["identity.frame_35e_wire_name"]["effective_value"] == "DY001"
    assert fields["identity.pack_number"]["effective_value"] == "001"
    assert fields["battery.installed_capacity"]["effective_value"] == 230.0
    assert fields["mos.charge_closed"]["effective_value"] is False
    assert fields["system.operation_mode"]["effective_value"] == "standstill"
    assert fields["system.substate_raw"]["effective_value"] == 0x0012
    assert fields["history.overdischarge_count"]["effective_value"] == 26
    assert snapshot["health"]["high_cvl_zero_ccl"] is True
    assert snapshot["health"]["stock_victron_identity_hazard"] is True
    assert snapshot["health"]["charge_path_state"] == "blocked_or_isolated"
    assert snapshot["health"]["control_ready"] is False


def test_preserved_capture_builds_safe_pc_only_mock_battery_contract():
    snapshot = replay([CAPTURE], 230.0).snapshot()
    model = build_mock_dbus_model(snapshot)
    paths = model["paths"]
    assert model["registered_on_dbus"] is False
    assert paths["/ProductName"] == "Deye LV battery"
    assert paths["/Dc/0/Voltage"] == 53.5
    assert paths["/Info/MaxChargeVoltage"] == 55.2
    assert paths["/Info/MaxChargeCurrent"] == 0.0
    assert paths["/Info/MaxDischargeCurrent"] == 230.0
    assert paths["/Io/AllowToCharge"] == 0
    assert paths["/Io/AllowToDischarge"] == 1
    assert paths["/InstalledCapacity"] == 230.0
    assert paths["/Capacity"] == 230.0
    assert paths["/ConsumedAmphours"] == 0.0
    assert paths["/System/MaxVoltageCellId"] is None
    assert paths["/System/MinVoltageCellId"] is None
    assert paths["/Diagnostics/Policy/WritesVebusMode"] == 0
    assert model["policy"]["production_control_allowed"] is False
