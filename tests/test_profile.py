"""BMS-side protocol profile detection and victronCAN decoding."""

from pathlib import Path

import pytest

from deye_virtual_battery.cache import (
    SIGN_CROSSCHECK_DISAGREEMENTS,
    VirtualBatteryCache,
)
from deye_virtual_battery.candump import CandumpParseError, parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.decoder import DeyeDecoder, decode_victron_alarm_frame
from deye_virtual_battery.policy import (
    COMMON_REQUIRED_CONTROL_FIELDS,
    REQUIRED_CONTROL_FIELDS,
    required_control_fields,
)
from deye_virtual_battery.profile import (
    DEYE_EXCLUSIVE_IDS,
    DEYE_NATIVE,
    PROFILE_NEUTRAL_IDS,
    UNKNOWN,
    VICTRON_CAN,
    VICTRON_EXCLUSIVE_IDS,
    ProtocolProfileDetector,
    is_profile_evidence,
)
from deye_virtual_battery.stateful import StatefulShadow


TRANSITION_CAPTURE = (
    Path(__file__).resolve().parent / "data" / "deye-profile-switch-window.log"
)

# Live 2026-09-01 profile-switch timestamps, in seconds since the epoch.
VICTRON_FIRST_MARKER = 1788287609.065572
VICTRON_LAST_MARKER = 1788287638.066745


def frame(can_id: str, payload: str, timestamp: float, direction: str = "R"):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} {direction}")


def values(can_frame):
    return {field.name: field for field in DeyeDecoder().decode(can_frame)}


# One second of live Deye-native traffic recorded at 2026-09-01 18:33:07 UTC.
DEYE_CYCLE = (
    ("110", "0000000000000031"),
    ("150", "1502F8FFE603E803"),
    ("200", "110D070DFA00F000"),
    ("250", "0E0170FE0000E600"),
    ("351", "48020000FC08E001"),
    ("355", "6400640000000000"),
    ("356", "D214F8FFF0000000"),
    ("359", "0000000000000000"),
    ("35C", "C000000000000000"),
    ("35E", "44593030311CFC08"),
    ("361", "110D070DFA00F000"),
    ("363", "F005F00500000000"),
    ("364", "0100000001000000"),
    ("371", "0000FC0800000000"),
    ("400", "0000000000000300"),
)

# One second of live victronCAN traffic recorded at 2026-09-01 18:33:35 UTC.
# The Deye vendor frames are gone, 0x35A/0x35F have appeared, 0x35E now says
# PYLON, and the 0x356 current has the opposite sign to the 0x150 current.
VICTRON_CYCLE = (
    ("110", "0000000000000031"),
    ("150", "1502F8FFE603E803"),
    ("200", "110D070DFA00F000"),
    ("250", "0E0170FE0A00E600"),
    ("351", "48026400FC08E001"),
    ("355", "6400640000000000"),
    ("356", "D2140800F0000000"),
    ("35A", "0000000000000000"),
    ("35E", "50594C4F4E1CFC08"),
    ("35F", "001CF005FC084459"),
    ("400", "0000000000000300"),
)


def apply_cycle(cache: VirtualBatteryCache, cycle, timestamp: float) -> None:
    for index, (can_id, payload) in enumerate(cycle):
        cache.apply(frame(can_id, payload, timestamp + index * 0.01))


def run_cycles(cache: VirtualBatteryCache, cycle, start: float, count: int) -> float:
    timestamp = start
    for _ in range(count):
        apply_cycle(cache, cycle, timestamp)
        timestamp += 1.0
    return timestamp - 1.0


def test_profile_evidence_and_neutral_frames_never_overlap():
    evidence = DEYE_EXCLUSIVE_IDS | VICTRON_EXCLUSIVE_IDS | {0x35E}
    assert evidence & PROFILE_NEUTRAL_IDS == frozenset()
    assert all(is_profile_evidence(can_id) for can_id in evidence)
    assert not any(is_profile_evidence(can_id) for can_id in PROFILE_NEUTRAL_IDS)
    # Every frame carrying control or measurement data must be neutral, so a
    # profile change can never invalidate it.
    assert {0x351, 0x355, 0x356, 0x110} <= PROFILE_NEUTRAL_IDS


def test_detector_acquires_each_profile_from_its_exclusive_frames():
    detector = ProtocolProfileDetector()
    assert detector.resolve(0.0) == UNKNOWN

    detector.observe(0x359, bytes(8), 10.0)
    assert detector.resolve(10.0) == DEYE_NATIVE

    other = ProtocolProfileDetector()
    other.observe(0x35F, bytes.fromhex("001CF005FC084459"), 10.0)
    assert other.resolve(10.0) == VICTRON_CAN


def test_manufacturer_name_alone_identifies_the_profile():
    detector = ProtocolProfileDetector()
    detector.observe(0x35E, bytes.fromhex("50594C4F4E1CFC08"), 5.0)
    assert detector.resolve(5.0) == VICTRON_CAN
    detector.observe(0x35E, bytes.fromhex("44593030311CFC08"), 6.0)
    # A single contradicting frame while the other family is still fresh must
    # not move the profile.
    assert detector.resolve(6.0) == VICTRON_CAN


def test_single_stray_frame_cannot_switch_an_established_profile():
    detector = ProtocolProfileDetector()
    for offset in range(4):
        detector.observe(0x359, bytes(8), 100.0 + offset)
    assert detector.resolve(103.0) == DEYE_NATIVE

    detector.observe(0x35A, bytes(8), 103.2)
    assert detector.resolve(103.2) == DEYE_NATIVE

    # Only once the Deye family goes quiet and the victronCAN family repeats
    # does the profile follow.
    detector.observe(0x35A, bytes(8), 104.2)
    detector.observe(0x35F, bytes.fromhex("001CF005FC084459"), 104.3)
    assert detector.resolve(104.3) == VICTRON_CAN


def test_profile_survives_a_short_silence_and_expires_after_the_hold_window():
    detector = ProtocolProfileDetector()
    detector.observe(0x371, bytes(8), 200.0)
    assert detector.resolve(205.0) == DEYE_NATIVE
    assert detector.resolve(215.0) == UNKNOWN


def test_deye_profile_inverts_the_wire_current_and_victroncan_does_not():
    deye = VirtualBatteryCache()
    at = run_cycles(deye, DEYE_CYCLE, 1000.0, 3)
    deye_fields = deye.snapshot(at=at + 0.2)["fields"]
    assert deye_fields["battery.current_raw_deye"]["effective_value"] == -0.8
    assert deye_fields["battery.current"]["effective_value"] == 0.8

    victron = VirtualBatteryCache()
    at = run_cycles(victron, VICTRON_CYCLE, 1000.0, 3)
    victron_fields = victron.snapshot(at=at + 0.2)["fields"]
    assert victron_fields["battery.current_raw_deye"]["effective_value"] == 0.8
    assert victron_fields["battery.current"]["effective_value"] == 0.8


def test_unresolved_profile_publishes_no_victron_convention_current():
    cache = VirtualBatteryCache()
    cache.apply(frame("356", "D214F8FFF0000000", 1000.0))
    fields = cache.snapshot(at=1000.1)["fields"]
    assert fields["battery.current_raw_deye"]["effective_value"] == -0.8
    assert fields["battery.current"]["effective_value"] is None
    assert fields["battery.power_candidate"]["effective_value"] is None


def test_victroncan_profile_requires_only_the_common_control_fields():
    assert required_control_fields(VICTRON_CAN) == COMMON_REQUIRED_CONTROL_FIELDS
    assert required_control_fields(DEYE_NATIVE) == REQUIRED_CONTROL_FIELDS
    assert required_control_fields(UNKNOWN) == REQUIRED_CONTROL_FIELDS
    # The Deye-only array limits and 0x359 tables must never be demanded from a
    # profile that does not transmit them.
    assert "limits.array_max_charge_current" not in COMMON_REQUIRED_CONTROL_FIELDS
    assert "alarms.active_v33_conditions" not in COMMON_REQUIRED_CONTROL_FIELDS
    # 0x110 is transmitted by both profiles, so pack conditions and MOS state
    # stay mandatory everywhere.
    assert "pack.active_v33_conditions" in COMMON_REQUIRED_CONTROL_FIELDS
    assert "mos.charge_closed" in COMMON_REQUIRED_CONTROL_FIELDS


def test_victroncan_publishes_limits_without_the_array_frames():
    cache = VirtualBatteryCache()
    at = run_cycles(cache, VICTRON_CYCLE, 2000.0, 4)
    model = build_mock_dbus_model(cache.snapshot(at=at + 0.2))
    policy = model["policy"]

    assert policy["bms_protocol_profile"] == VICTRON_CAN
    assert policy["critical_data_ready"] is True
    assert policy["missing_or_stale_control_fields"] == []
    assert policy["raw_limits"]["array_ccl_a"] is None
    assert policy["raw_limits"]["pack_allowable_ccl_a"] == 10.0
    assert policy["effective_limits"]["ccl_a"] == 10.0
    assert policy["effective_limits"]["dcl_a"] == 230.0
    assert model["paths"]["/Diagnostics/Deye/ArrayLimitsAvailable"] == 0
    # Module counts come from the Deye-only 0x364 and must be invalid rather
    # than an invented zero.
    assert model["paths"]["/System/NrOfModulesOnline"] is None


def test_pack_allowable_charge_current_restricts_but_never_discharge():
    cycle = list(VICTRON_CYCLE)
    cycle[3] = ("250", "0E0170FE0200E600")  # pack allows only 2 A charge
    cache = VirtualBatteryCache()
    at = run_cycles(cache, tuple(cycle), 2000.0, 4)
    policy = build_mock_dbus_model(cache.snapshot(at=at + 0.2))["policy"]
    assert policy["raw_limits"]["ccl_a"] == 10.0
    assert policy["raw_limits"]["pack_allowable_ccl_a"] == 2.0
    # 0x250 is quantized to whole amps, so its ceiling rather than its floor is
    # applied; it still cuts the 10 A request down to the pack's own limit.
    assert policy["effective_limits"]["ccl_a"] == 3.0
    assert policy["effective_limits"]["dcl_a"] == 230.0


def test_whole_amp_quantization_of_0x250_never_shaves_a_normal_limit():
    # A pack limit one tenth below the 0x351 request is reported by 0x250 as a
    # floor.  The cross-check must not turn that into a real reduction.
    cycle = list(VICTRON_CYCLE)
    cycle[3] = ("250", "0E0170FE3700E600")  # reports 55 A for a true 55.2 A
    cycle[4] = ("351", "48022802FC08E001")  # CCL 55.2 A
    cache = VirtualBatteryCache()
    at = run_cycles(cache, tuple(cycle), 2500.0, 4)
    policy = build_mock_dbus_model(cache.snapshot(at=at + 0.2))["policy"]
    assert policy["raw_limits"]["ccl_a"] == 55.2
    assert policy["effective_limits"]["ccl_a"] == 55.2


def test_all_zero_victron_alarm_frame_means_unsupported_not_ok():
    states = decode_victron_alarm_frame(bytes(8))
    assert set(states.values()) == {"not_supported"}

    decoded = values(frame("35A", "0000000000000000", 1000.0))
    assert decoded["victron_alarms.supported_fields"].value == []
    assert decoded["victron_alarms.any_supported_field"].value is False
    assert decoded["victron_alarms.high_voltage_alarm"].value == "not_supported"


def test_victron_alarm_bits_use_the_specified_tristate_encoding():
    # Byte 0 = 0b00000110: high-voltage alarm active (0b01 at bits 2-3),
    # low-voltage alarm inactive (0b10 at bits 4-5) reads as 0b00 here, so use
    # an explicit pattern instead: bits 2-3 = 01, bits 4-5 = 10.
    payload = bytes([0b00100100, 0, 0, 0, 0, 0, 0, 0]).hex().upper()
    states = decode_victron_alarm_frame(bytes.fromhex(payload))
    assert states["high_voltage_alarm"] == "active"
    assert states["low_voltage_alarm"] == "inactive"
    assert states["general_alarm"] == "not_supported"


def test_active_victron_alarm_raises_the_mapped_venus_path():
    cycle = list(VICTRON_CYCLE)
    cycle[7] = ("35A", bytes([0b00100100, 0, 0, 0, 0, 0, 0, 0]).hex().upper())
    cache = VirtualBatteryCache()
    at = run_cycles(cache, tuple(cycle), 3000.0, 4)
    model = build_mock_dbus_model(cache.snapshot(at=at + 0.2))

    assert model["paths"]["/Alarms/HighVoltage"] == 2
    assert model["paths"]["/Alarms/LowVoltage"] == 0
    # Victron is explicit that alarm data never controls charge or discharge.
    assert model["policy"]["effective_limits"]["ccl_a"] == 10.0
    assert model["policy"]["effective_limits"]["dcl_a"] == 230.0
    assert model["policy"]["permissions"]["allow_discharge"] is True


def test_victron_capacity_field_conflict_is_recorded_not_published():
    decoded = values(frame("35F", "001CF005FC084459", 1000.0))
    assert decoded["victron_identity.online_capacity_raw"].value == 2300
    victron_scaled = decoded["victron_identity.online_capacity_ah_victron_scaling"]
    assert victron_scaled.value == 2300.0
    assert victron_scaled.valid is False
    assert decoded["victron_identity.online_capacity_ah_deye_scaling"].value == 230.0
    assert decoded["victron_identity.firmware_version_big_endian"].value == 0xF005

    cache = VirtualBatteryCache()
    at = run_cycles(cache, VICTRON_CYCLE, 4000.0, 3)
    model = build_mock_dbus_model(cache.snapshot(at=at + 0.2))
    assert model["paths"]["/InstalledCapacity"] == 230.0
    assert model["paths"]["/Capacity"] == 230.0


def test_wire_identity_pylon_never_changes_the_published_identity():
    cache = VirtualBatteryCache()
    at = run_cycles(cache, VICTRON_CYCLE, 5000.0, 3)
    model = build_mock_dbus_model(cache.snapshot(at=at + 0.2))
    assert model["paths"]["/Diagnostics/Profile/WireIdentityClaimsPylontech"] == 1
    assert model["paths"]["/ProductId"] == 0xFFFF
    assert model["paths"]["/Manufacturer"] == "Deye"
    # v0.71 selects Pylontech only from the 0x359 "PN" marker, which this
    # profile never sends, so the stock decoder would still choose LG.
    assert cache.snapshot(at=at + 0.2)["health"]["stock_victron_identity_hazard"] is True


def test_sustained_sign_disagreement_inhibits_charge_without_touching_discharge():
    # Force a contradiction by keeping the Deye profile active while 0x356
    # carries the opposite sign to 0x150 at a clearly non-zero current.
    cycle = list(DEYE_CYCLE)
    cycle[1] = ("150", "1502C0FFE603E803")  # 0x150 reads -6.4 A (charging)
    cycle[6] = ("356", "D2144000F0000000")  # 0x356 reads +6.4 A on the wire
    cache = VirtualBatteryCache()
    # The cross-check is suppressed during the post-acquisition settling
    # window, so run well past it before expecting a verdict.
    at = run_cycles(cache, tuple(cycle), 6000.0, SIGN_CROSSCHECK_DISAGREEMENTS + 8)
    snapshot = cache.snapshot(at=at + 0.2)

    assert cache.sign_convention_conflict is True
    assert snapshot["health"]["current_sign_convention_conflict"] is True
    model = build_mock_dbus_model(snapshot)
    assert model["paths"]["/Diagnostics/Alarms/CurrentSignConflict"] == 2
    # The service stays online and keeps publishing telemetry; only charge is
    # inhibited, because a synthetic discharge stop could cut AC output.
    assert model["paths"]["/Connected"] == 1
    assert model["paths"]["/Dc/0/Current"] is not None
    assert (
        "current_sign_convention_conflict"
        in model["policy"]["permissions"]["charge_inhibit_reasons"]
    )
    assert model["policy"]["effective_limits"]["ccl_a"] == 0.0
    assert model["policy"]["effective_limits"]["dcl_a"] == 230.0


def test_agreeing_signs_never_raise_the_conflict():
    cache = VirtualBatteryCache()
    cycle = list(DEYE_CYCLE)
    cycle[1] = ("150", "1502C0FFE603E803")
    cycle[6] = ("356", "D214C0FFF0000000")
    run_cycles(cache, tuple(cycle), 7000.0, SIGN_CROSSCHECK_DISAGREEMENTS + 8)
    assert cache.sign_convention_conflict is False
    assert cache.sign_crosscheck_agreements > 0


def test_simulated_profile_switch_keeps_the_service_connected():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    timestamp = 8000.0
    for _ in range(6):
        apply_cycle(cache, DEYE_CYCLE, timestamp)
        model = shadow.step(cache.snapshot(at=timestamp + 0.5), timestamp=timestamp + 0.5)
        timestamp += 1.0
    assert model["paths"]["/Connected"] == 1
    assert model["paths"]["/Diagnostics/Profile/BmsProtocol"] == DEYE_NATIVE

    for _ in range(6):
        apply_cycle(cache, VICTRON_CYCLE, timestamp)
        model = shadow.step(cache.snapshot(at=timestamp + 0.5), timestamp=timestamp + 0.5)
        assert model["paths"]["/Connected"] == 1, model["lifecycle"]
        timestamp += 1.0
    assert model["paths"]["/Diagnostics/Profile/BmsProtocol"] == VICTRON_CAN

    for _ in range(6):
        apply_cycle(cache, DEYE_CYCLE, timestamp)
        model = shadow.step(cache.snapshot(at=timestamp + 0.5), timestamp=timestamp + 0.5)
        assert model["paths"]["/Connected"] == 1, model["lifecycle"]
        timestamp += 1.0
    assert model["paths"]["/Diagnostics/Profile/BmsProtocol"] == DEYE_NATIVE


def _replay(path: Path, start: float, end: float):
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    samples = []
    last_step = None
    with path.open(encoding="ascii", errors="replace") as stream:
        for line in stream:
            try:
                can_frame = parse_candump_line(line)
            except CandumpParseError:
                continue
            if not start <= can_frame.timestamp <= end:
                continue
            cache.apply(can_frame)
            if last_step is None or can_frame.timestamp - last_step >= 0.5:
                last_step = can_frame.timestamp
                samples.append(
                    (
                        can_frame.timestamp,
                        shadow.step(
                            cache.snapshot(at=can_frame.timestamp),
                            timestamp=can_frame.timestamp,
                        ),
                    )
                )
    return cache, samples


@pytest.mark.skipif(
    not TRANSITION_CAPTURE.exists(), reason="local victronCAN capture not present"
)
def test_real_profile_switch_never_disconnects_the_battery_service():
    cache, samples = _replay(TRANSITION_CAPTURE, 1788287460.0, 1788287820.0)

    profiles = [
        (event["previous"], event["current"])
        for event in cache.protocol_events
        if event["field"] == "protocol.bms_profile"
    ]
    assert profiles == [
        (UNKNOWN, DEYE_NATIVE),
        (DEYE_NATIVE, VICTRON_CAN),
        (VICTRON_CAN, DEYE_NATIVE),
    ]

    online = [
        sample
        for sample in samples
        if sample[1]["lifecycle"]["state"] == "online"
    ]
    # The service qualifies once, early, and never leaves the online state.
    assert online[0][0] < VICTRON_FIRST_MARKER
    disconnected = [
        (timestamp, model["lifecycle"]["state"])
        for timestamp, model in samples
        if timestamp > online[0][0] and model["paths"]["/Connected"] != 1
    ]
    assert disconnected == []


@pytest.mark.skipif(
    not TRANSITION_CAPTURE.exists(), reason="local victronCAN capture not present"
)
def test_real_capture_current_sign_follows_the_active_profile():
    _, samples = _replay(TRANSITION_CAPTURE, 1788287460.0, 1788287660.0)

    def currents(predicate):
        return [
            (model["paths"]["/Dc/0/Current"], model["paths"]["/Diagnostics/Deye/RawCcl"])
            for timestamp, model in samples
            if predicate(timestamp) and model["paths"]["/Dc/0/Current"] is not None
        ]

    during = currents(
        lambda timestamp: VICTRON_FIRST_MARKER + 7 < timestamp < VICTRON_LAST_MARKER - 2
    )
    before = currents(lambda timestamp: 1788287500 < timestamp < 1788287600)

    # The battery was drifting at well under one amp in both profiles.  With
    # the wire signs opposite, only a profile-aware conversion can report the
    # same direction on both sides of the switch.
    assert during and before
    assert all(current > 0 for current, _ in during)
    assert all(current > 0 for current, _ in before)
    # The victronCAN profile raised the charge current limit from 0 A to 10 A.
    assert {limit for _, limit in during} == {10.0}
    assert {limit for _, limit in before} == {0.0}
