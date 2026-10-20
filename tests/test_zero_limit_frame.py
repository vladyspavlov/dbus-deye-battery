"""A 0x351 frame whose own voltage limits are impossible is not a request.

Issue #7: a two-pack SE-F5 Pro-C bank interleaved an all-zero ``0x351`` with
its real one, every 5.35 s on a clock of its own.  Taken at face value it
published DCL 0 A for about a second at a time, which can stop an inverter
running off-grid, and its CCL 0 A held CVL at the blocked-charge value while
the bank was charging.  The whole frame is now refused.
"""

from __future__ import annotations

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.decoder import DeyeDecoder
from deye_virtual_battery.policy import PolicyConfig, evaluate_policy
from deye_virtual_battery.stateful import StatefulShadow
from tests.test_multi_pack import Bank, Pack

ZERO = "0000000000000000"
LIMIT_FIELDS = (
    "limits.max_charge_voltage",
    "limits.max_charge_current",
    "limits.max_discharge_current",
    "limits.battery_low_voltage",
)


def line(payload: str, timestamp: float = 1.0) -> str:
    return f"({timestamp:.6f}) can0 351#{payload} R"


def decode(payload: str) -> dict:
    fields = DeyeDecoder().decode(parse_candump_line(line(payload)))
    return {item.name: item for item in fields}


def charging_bank() -> Bank:
    # Two packs well below full, both accepting charge.
    return Bank(
        packs=[Pack(soc=78.0, deye_current=-12.0), Pack(soc=78.0, deye_current=-12.0)],
        ccl=45.0,
        deye_current=-24.0,
    )


def run(bank: Bank, seconds: float, *, zero_every: float | None = None,
        zero_only_after: float | None = None, extra: dict[float, str] | None = None):
    """Step the stateful model every 250 ms, as the publisher does."""

    start = 1000.0
    lines: list[tuple[float, str]] = []
    for second in range(int(seconds) + 1):
        timestamp = start + second
        for text in bank.frames(timestamp):
            if zero_only_after is not None and second >= zero_only_after and "351#" in text:
                continue
            lines.append((float(text[1:text.index(")")]), text))
    if zero_every is not None:
        at = start + 2.37
        while at < start + seconds:
            lines.append((at, line(ZERO, at)))
            at += zero_every
    for at, payload in (extra or {}).items():
        lines.append((start + at, line(payload, start + at)))
    lines.sort()

    cache = VirtualBatteryCache()
    shadow = StatefulShadow(policy_config=PolicyConfig())
    samples = []
    tick = start + 0.25
    for at, text in lines:
        while at >= tick:
            samples.append(shadow.step(cache.snapshot(at=tick), timestamp=tick))
            tick += 0.25
        cache.apply(parse_candump_line(text))
    return cache, samples


def published(samples: list[dict], path: str) -> set:
    return {sample["paths"][path] for sample in samples if sample["paths"]["/Connected"] == 1}


# -- decoding --------------------------------------------------------------


def test_an_all_zero_frame_is_refused_as_a_whole():
    fields = decode(ZERO)
    assert not any(fields[name].valid for name in LIMIT_FIELDS)
    assert fields["diagnostics.high_cvl_zero_ccl"].valid is False


def test_one_impossible_voltage_refuses_the_currents_with_it():
    # CVL 58.4 V is plausible, but a low-voltage limit of 0 V is not.
    fields = decode("48020000D0070000")
    assert fields["limits.max_charge_voltage"].valid is True
    assert fields["limits.max_charge_current"].valid is False
    assert fields["limits.max_discharge_current"].valid is False


def test_a_stop_with_valid_voltages_is_still_a_stop():
    # A real BMS stop keeps its voltage limits and zeroes the currents.
    fields = decode("480200000000E001")
    assert all(fields[name].valid for name in LIMIT_FIELDS)
    assert fields["limits.max_charge_current"].value == 0.0
    assert fields["limits.max_discharge_current"].value == 0.0


# -- cache and model -------------------------------------------------------


def test_the_cache_keeps_the_previous_limits_and_counts_the_refusal():
    cache, at = charging_bank().cache()
    cache.apply(parse_candump_line(line(ZERO, at)))
    policy = evaluate_policy(cache.snapshot(at=at + 0.1))
    assert policy["raw_limits"]["ccl_a"] == 45.0
    assert policy["raw_limits"]["dcl_a"] == 200.0
    assert policy["effective_limits"]["dcl_a"] == 200.0
    model = build_mock_dbus_model(cache.snapshot(at=at + 0.1))
    assert model["paths"]["/Diagnostics/Deye/RejectedLimitFrames"] == 1


def test_a_healthy_bank_reports_no_refused_frames():
    assert charging_bank().model()["paths"]["/Diagnostics/Deye/RejectedLimitFrames"] == 0


# -- what the GX sees over time --------------------------------------------


def test_interleaved_zero_frames_do_not_move_the_published_limits():
    baseline = run(charging_bank(), 60)[1]
    cache, samples = run(charging_bank(), 60, zero_every=5.35)
    assert cache.rejected_limit_frames >= 10
    for path in ("/Info/MaxChargeVoltage", "/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent"):
        assert published(samples, path) == published(baseline, path)
        assert len(published(samples, path)) == 1
    assert published(samples, "/Info/MaxChargeVoltage") == {57.2}
    assert published(samples, "/Info/MaxDischargeCurrent") == {200.0}


def test_a_one_frame_stop_with_valid_voltages_still_reaches_the_gx():
    _, samples = run(charging_bank(), 20, extra={10.5: "480200000000E001"})
    assert 0.0 in published(samples, "/Info/MaxDischargeCurrent")
    assert 0.0 in published(samples, "/Info/MaxChargeCurrent")


def test_only_refused_frames_age_out_like_missing_limits():
    # The real 0x351 stops at 20 s while the all-zero one keeps arriving.
    _, samples = run(charging_bank(), 40, zero_every=1.0, zero_only_after=20)
    states = [sample["lifecycle"]["state"] for sample in samples]
    assert "critical_data_stale" in states
    stale = [sample for sample in samples if sample["lifecycle"]["state"] == "critical_data_stale"]
    assert all(sample["paths"]["/Connected"] == 0 for sample in stale)
    # The junk never became a published 0 A stop.
    assert 0.0 not in published(samples, "/Info/MaxDischargeCurrent")
