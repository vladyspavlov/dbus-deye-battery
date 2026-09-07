from deye_virtual_battery.cache import TIMEOUTS, VirtualBatteryCache
from deye_virtual_battery.can_keepalive import KEEPALIVE_FRAMES
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.decoder import CRITICAL, TRANSPORT
from deye_virtual_battery.policy import PolicyConfig, evaluate_policy
from deye_virtual_battery.stateful import StatefulConfig, StatefulShadow
from deye_virtual_battery.venus_bms_publisher import _fixed_paths


def frame(can_id: str, payload: str, timestamp: float):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} R")


def add_cycle(cache: VirtualBatteryCache, timestamp: float, *, warning: bool = False) -> None:
    condition = "0000000001000031" if warning else "0000000000000031"
    payloads = (
        ("110", condition),
        ("200", "2D0D080DE600E600"),
        ("351", "48024408FC08E001"),
        ("355", "6400640000000000"),
        ("356", "D2140000E6000000"),
        ("359", "0000000000000000"),
        ("35E", "44593030311CFC08"),
        ("361", "2D0D080DE600E600"),
        ("364", "0101000001000000"),
        ("371", "4408FC0800000000"),
        ("400", "0000000000001200"),
    )
    for index, (can_id, payload) in enumerate(payloads):
        cache.apply(frame(can_id, payload, timestamp + index * 0.01))


def test_exact_victron_keepalive_pair_and_period_contract():
    assert KEEPALIVE_FRAMES == (
        (0x305, bytes(8)),
        (0x307, bytes.fromhex("1234567856494300")),
    )


def test_control_and_transport_data_expire_by_victron_three_second_deadline():
    assert TIMEOUTS[CRITICAL] == 3.0
    assert TIMEOUTS[TRANSPORT] == 3.0
    assert StatefulConfig().can_alive_timeout_seconds == 3.0


def test_standard_warning_is_immediate_but_does_not_change_limits():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()
    add_cycle(cache, 1000.0)
    shadow.step(cache.snapshot(at=1000.2), timestamp=1000.2)
    add_cycle(cache, 1002.2)
    shadow.step(cache.snapshot(at=1002.4), timestamp=1002.4)

    add_cycle(cache, 1002.5, warning=True)
    warned = shadow.step(cache.snapshot(at=1002.7), timestamp=1002.7)
    assert warned["paths"]["/Alarms/HighCellVoltage"] == 1
    assert warned["paths"]["/Info/MaxChargeCurrent"] == 211.6
    assert warned["paths"]["/Info/MaxDischargeCurrent"] == 230.0

    add_cycle(cache, 1002.8)
    cleared = shadow.step(cache.snapshot(at=1003.0), timestamp=1003.0)
    assert cleared["paths"]["/Alarms/HighCellVoltage"] == 0


def test_blocked_charge_cvl_is_fixed_not_pack_voltage_minus_offset():
    config = PolicyConfig()
    values = []
    for payload in ("D2140000E6000000", "AE150000E6000000"):
        cache = VirtualBatteryCache()
        add_cycle(cache, 2000.0)
        cache.apply(frame("110", "0000000000000021", 2000.2))
        cache.apply(frame("351", "48020000FC08E001", 2000.21))
        cache.apply(frame("371", "0000FC0800000000", 2000.22))
        cache.apply(frame("356", payload, 2000.23))
        values.append(
            evaluate_policy(cache.snapshot(at=2000.3), config=config)[
                "effective_limits"
            ]["cvl_v"]
        )
    assert values == [55.2, 55.2]


def test_capability_does_not_claim_nonzero_ccl_at_full_charge():
    fixed = _fixed_paths(PolicyConfig())
    assert fixed["/Capabilities/ChargeVoltageControl"] == 0
    assert fixed["/Diagnostics/Safety/AlarmFlagsControlLimits"] == 0


def test_cvl_lowers_immediately_but_requires_twenty_stable_seconds_to_raise():
    cache = VirtualBatteryCache()
    shadow = StatefulShadow()

    # Qualify while charge is blocked, establishing the fixed lower CVL.
    add_cycle(cache, 3000.0)
    cache.apply(frame("110", "0000000000000021", 3000.2))
    cache.apply(frame("351", "48020000FC08E001", 3000.21))
    cache.apply(frame("371", "0000FC0800000000", 3000.22))
    shadow.step(cache.snapshot(at=3000.3), timestamp=3000.3)
    add_cycle(cache, 3002.4)
    cache.apply(frame("110", "0000000000000021", 3002.6))
    cache.apply(frame("351", "48020000FC08E001", 3002.61))
    cache.apply(frame("371", "0000FC0800000000", 3002.62))
    blocked = shadow.step(cache.snapshot(at=3002.7), timestamp=3002.7)
    assert blocked["paths"]["/Info/MaxChargeVoltage"] == 55.2

    # A positive-CCl/closed-MOS pulse may raise the target, never the applied
    # value, until it remains stable for the qualification interval.
    add_cycle(cache, 3003.0)
    pending = shadow.step(cache.snapshot(at=3003.2), timestamp=3003.2)
    assert pending["paths"]["/Diagnostics/Cvl/Target"] == 57.2
    assert pending["paths"]["/Info/MaxChargeVoltage"] == 55.2
    assert pending["paths"]["/Diagnostics/Cvl/RaisePending"] == 1

    add_cycle(cache, 3023.3)
    raised = shadow.step(cache.snapshot(at=3023.5), timestamp=3023.5)
    assert raised["paths"]["/Info/MaxChargeVoltage"] == 57.2
    assert raised["paths"]["/Diagnostics/Cvl/RaisePending"] == 0

    # A new block lowers the published CVL without delay.
    add_cycle(cache, 3023.6)
    cache.apply(frame("110", "0000000000000021", 3023.8))
    cache.apply(frame("351", "48020000FC08E001", 3023.81))
    cache.apply(frame("371", "0000FC0800000000", 3023.82))
    lowered = shadow.step(cache.snapshot(at=3023.9), timestamp=3023.9)
    assert lowered["paths"]["/Info/MaxChargeVoltage"] == 55.2
