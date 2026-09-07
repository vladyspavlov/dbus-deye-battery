"""The pack serial arrives split across two CAN frames.

`0x600` carries the first eight characters and `0x650` the second eight.
Joined, they are the serial the vendor app shows, and Venus wants it on the
standard `/Serial` path. It also identifies one physical battery, so the
offline tooling must not print it into output people share.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.cli import REDACTED, redact_serial, replay
from deye_virtual_battery.dbus_model import build_mock_dbus_model


DATA = Path(__file__).resolve().parent / "data"
SAMPLE = DATA / "deye-se-f12c-sample-5min.log"


def frame(can_id: str, payload: str, timestamp: float = 1000.0):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} R")


def model_from(*frames, timestamp: float = 1000.0):
    cache = VirtualBatteryCache()
    for can_frame in frames:
        cache.apply(can_frame)
    return build_mock_dbus_model(cache.snapshot(at=timestamp))


# "DEYE1234" and "5678ABCD"
FIRST = "4445594531323334"
SECOND = "3536373841424344"


def test_the_two_halves_join_in_frame_order():
    model = model_from(frame("600", FIRST), frame("650", SECOND))
    assert model["paths"]["/Serial"] == "DEYE12345678ABCD"


def test_half_a_serial_is_never_published():
    """A partial serial is worse than none: it looks like a whole one."""
    assert model_from(frame("600", FIRST))["paths"]["/Serial"] is None
    assert model_from(frame("650", SECOND))["paths"]["/Serial"] is None


def test_no_serial_frames_publishes_the_venus_invalid_value():
    assert model_from(frame("355", "6400640000000000"))["paths"]["/Serial"] is None


def test_a_non_ascii_half_is_rejected_rather_than_mangled():
    model = model_from(frame("600", "00FF00FF00FF00FF"), frame("650", SECOND))
    assert model["paths"]["/Serial"] is None


def test_vendor_padding_is_trimmed():
    padded = "4445594531323334"  # "DEYE1234"
    spaces = "2020202020202020"  # eight spaces
    assert model_from(frame("600", padded), frame("650", spaces))["paths"][
        "/Serial"
    ] == "DEYE1234"


def test_an_all_blank_serial_is_treated_as_absent():
    spaces = "2020202020202020"
    assert model_from(frame("600", spaces), frame("650", spaces))["paths"]["/Serial"] is None


def test_the_bundled_capture_yields_a_full_sixteen_character_serial():
    model = build_mock_dbus_model(replay([SAMPLE], 230.0).snapshot())
    serial = model["paths"]["/Serial"]
    assert isinstance(serial, str) and len(serial) == 16


# --- redaction of shared output -----------------------------------------


def test_redaction_reaches_a_model_nested_inside_a_result():
    """Results embed a whole D-Bus model; a top-level-only pass would miss it."""
    payload = {"final_model": {"paths": {"/Serial": "DEYE12345678ABCD"}}}
    redact_serial(payload)
    assert payload["final_model"]["paths"]["/Serial"] == REDACTED


def test_redaction_covers_every_value_bearing_key_of_a_cached_field():
    state = {
        "value": "DEYE1234",
        "cached_value": "DEYE1234",
        "effective_value": "DEYE1234",
        "last_rejected_value": "DEYE1234",
        "updates": 140,
    }
    redact_serial({"fields": {"identity.serial_first_half": state}})
    assert [v for k, v in state.items() if k != "updates"] == [REDACTED] * 4
    assert state["updates"] == 140, "non-value keys must be left alone"


def test_show_serial_returns_the_payload_untouched():
    payload = {"paths": {"/Serial": "DEYE12345678ABCD"}}
    redact_serial(payload, show=True)
    assert payload["paths"]["/Serial"] == "DEYE12345678ABCD"


def test_redaction_survives_lists_and_non_dict_leaves():
    payload = {"samples": [{"paths": {"/Serial": "DEYE1234"}}, 7, "text", None]}
    redact_serial(payload)
    assert payload["samples"][0]["paths"]["/Serial"] == REDACTED


def run_cli(*argv):
    """Run the CLI as a user would, in a real subprocess."""
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    # The subprocess does not inherit pytest's pythonpath setting.
    environment["PYTHONPATH"] = str(root / "src")
    return subprocess.run(
        [sys.executable, "-m", "deye_virtual_battery", *argv],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=root,
        env=environment,
        check=True,
    ).stdout


def test_no_subcommand_prints_the_serial_by_default():
    """Replay output is what people paste into bug reports."""
    serial = build_mock_dbus_model(replay([SAMPLE], 230.0).snapshot())["paths"]["/Serial"]
    for argv in (
        ("replay", "--json", str(SAMPLE)),
        ("model", str(SAMPLE)),
    ):
        output = run_cli(*argv)
        assert serial not in output, f"{argv[0]} leaked the serial"
        assert REDACTED in output


def test_show_serial_opts_back_in():
    serial = build_mock_dbus_model(replay([SAMPLE], 230.0).snapshot())["paths"]["/Serial"]
    assert serial in json.loads(run_cli("model", "--show-serial", str(SAMPLE)))["paths"]["/Serial"]


def test_serial_is_registered_once_and_never_rewritten():
    """A path the update loop rewrites could change D-Bus type mid-flight.

    The serial is static device information, so it is registered at
    qualification from the qualified model and then left alone.
    """
    from deye_virtual_battery.policy import PolicyConfig
    from deye_virtual_battery.venus_bms_publisher import _add_paths, _fixed_paths

    class FakeService:
        def __init__(self):
            self.paths = {}

        def add_path(self, path, value):
            self.paths[path] = value

    service = FakeService()
    dynamic = _add_paths(
        service,
        {"/Serial": "IGNORED", "/Soc": 100.0},
        config=PolicyConfig(),
        serial="DEYE12345678ABCD",
    )
    assert service.paths["/Serial"] == "DEYE12345678ABCD"
    assert "/Serial" not in dynamic
    assert "/Soc" in dynamic
    assert _fixed_paths(PolicyConfig())["/Serial"] is None
