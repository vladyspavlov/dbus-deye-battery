"""Firmware that clears bit 10 of its CAN ids is read as documented.

Issue #7: a two-pack SE-F5 Pro-C bank sent every per-pack frame of 0x400 and
above with bit 10 cleared, so 0x550 arrived as 0x150, 0x600 as 0x200, 0x650
as 0x250 and pack 2's 0x751 as the bank's 0x351.  The same model with other
firmware sends the documented ids, so the remapping must stay idle until the
bank proves that it shifts, and never act on a bank that does not.
"""

from __future__ import annotations

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.id_layout import ShiftedIdLayout
from tests.test_multi_pack import Bank, Pack, _u16, _u32

SERIALS = ("TEST0000PACK0001", "TEST0000PACK0002")


def two_pack_bank() -> Bank:
    return Bank(
        packs=[
            Pack(deye_current=-12.0, cycles=38, charged_wh=157_967, discharged_wh=149_879,
                 serial=SERIALS[0]),
            Pack(deye_current=-6.0, cycles=2, charged_wh=8_423, discharged_wh=5_746,
                 serial=SERIALS[1]),
        ],
        deye_current=-18.0,
    )


def documented_lines(bank: Bank, timestamp: float) -> list[str]:
    """The bank's frames plus the ones the helper leaves out: 0x500, 0x700, 0x750."""

    lines = bank.frames(timestamp)
    for offset, _ in enumerate(bank.packs):
        extra = (
            (0x500 + offset, _u16(0x02E0) + bytes([0xAA]) + b"V1.0E"),
            (0x700 + offset, _u16(21) + _u16(0) + _u16(10) + _u16(0)),
            (0x750 + offset, bytes(8)),
        )
        for index, (can_id, payload) in enumerate(extra):
            lines.append(
                f"({timestamp + 0.5 + offset * 0.01 + index * 0.001:.6f}) can0 "
                f"{can_id:03X}#{payload.hex().upper()} R"
            )
    return lines


def shifted(line: str) -> str:
    """The same frame as firmware that clears bit 10 of the id sends it."""

    head, rest = line.split(" can0 ")
    can_id, tail = rest.split("#", 1)
    return f"{head} can0 {int(can_id, 16) & ~0x400:03X}#{tail}"


def run(bank: Bank, seconds: int, *, shift: bool) -> VirtualBatteryCache:
    cache = VirtualBatteryCache()
    for second in range(seconds):
        for line in documented_lines(bank, 1000.0 + second):
            cache.apply(parse_candump_line(shifted(line) if shift else line))
    return cache


def value(cache: VirtualBatteryCache, name: str):
    state = cache.fields.get(name)
    return None if state is None else state.cached_value


def frame(can_id: int, payload: bytes, timestamp: float = 1.0):
    return parse_candump_line(f"({timestamp:.6f}) can0 {can_id:03X}#{payload.hex().upper()} R")


def test_a_documented_bank_is_never_remapped():
    cache = run(two_pack_bank(), 6, shift=False)
    assert cache.id_layout.documented is True
    assert cache.id_layout.active is False
    assert cache.id_layout.remapped_frames == 0
    statistics = cache.snapshot(at=1005.9)["statistics"]
    assert statistics["shifted_id_layout"] is False
    assert statistics["shifted_id_frames"] == 0


def test_a_shifted_bank_reads_exactly_like_a_documented_one():
    documented = run(two_pack_bank(), 6, shift=False)
    shifted_cache = run(two_pack_bank(), 6, shift=True)
    assert shifted_cache.id_layout.active is True
    assert shifted_cache.id_layout.remapped_frames > 0
    for name in (
        "packs.1.diagnostics.current_victron_150",
        "packs.2.diagnostics.current_victron_150",
        "packs.1.diagnostics.voltage_150",
        "packs.1.diagnostics.soh_150",
        "packs.1.history.charged_energy",
        "packs.2.history.discharged_energy",
        "packs.1.identity.serial_second_half",
        "packs.2.identity.serial_first_half",
        "packs.1.battery.cycle_count",
        "packs.1.system.fault_level",
        "packs.1.history.short_circuit_count",
        "packs.1.history.charge_overcurrent_count",
        "limits.max_discharge_current",
    ):
        assert value(shifted_cache, name) == value(documented, name), name
    # The one frame that cannot be recovered: pack 2's 0x751 arrives on the
    # bank's 0x351, where it is refused as an impossible limit frame.
    assert value(documented, "packs.2.history.charge_overcurrent_count") == 0
    assert value(shifted_cache, "packs.2.history.charge_overcurrent_count") is None
    assert value(shifted_cache, "packs.1.diagnostics.current_victron_150") == 12.0


def test_shifted_bank_publishes_the_pack_currents_and_serials():
    cache = run(two_pack_bank(), 6, shift=True)
    paths = build_mock_dbus_model(cache.snapshot(at=1005.9))["paths"]
    assert paths["/Battery/1/Current"] == 12.0
    assert paths["/Battery/2/Current"] == 6.0
    assert paths["/Battery/1/Serial"] == SERIALS[0]
    assert paths["/Battery/2/Serial"] == SERIALS[1]
    assert paths["/History/ChargeCycles"] == 38
    assert paths["/Diagnostics/Deye/ShiftedIdLayout"] == 1
    assert paths["/Diagnostics/Deye/ShiftedIdFrames"] > 0
    # Pack 2's 0x751 still lands on 0x351 and is refused there.
    assert paths["/Info/MaxDischargeCurrent"] == 200.0


def test_one_frame_is_not_proof():
    layout = ShiftedIdLayout()
    version = frame(0x100, _u16(0x02E0) + bytes([0xAA]) + b"V1.0E")
    assert layout.normalise(version).can_id == 0x100
    assert layout.active is False
    assert layout.normalise(frame(0x000, bytes(8))).can_id == 0x000
    layout.normalise(frame(0x200, b"TEST0000", 2.0))
    assert layout.active is True
    assert layout.normalise(frame(0x000, bytes(8), 3.0)).can_id == 0x400


def test_a_documented_high_id_vetoes_the_shift_for_good():
    layout = ShiftedIdLayout()
    layout.normalise(frame(0x400, bytes(6) + _u16(3)))
    for timestamp in range(5):
        layout.normalise(frame(0x200, b"TEST0000", float(timestamp)))
    assert layout.active is False
    assert layout.normalise(frame(0x000, bytes(8))).can_id == 0x000


def test_the_proof_frames_cannot_be_real_cell_or_temperature_frames():
    layout = ShiftedIdLayout()
    cells = _u16(3343) + _u16(3331) + _u16(230) + _u16(220)
    temperatures = _u16(250) + (-400).to_bytes(2, "little", signed=True) + _u16(45) + _u16(100)
    for timestamp in range(10):
        layout.normalise(frame(0x200, cells, float(timestamp)))
        layout.normalise(frame(0x250, temperatures, float(timestamp)))
    assert layout.evidence == 0
    assert layout.active is False


def test_keepalive_ids_are_never_taken_for_shifted_counters():
    layout = ShiftedIdLayout(active=True)
    for can_id in (0x305, 0x306, 0x307):
        assert layout.normalise(frame(can_id, bytes(8))).can_id == can_id
    assert layout.normalise(frame(0x300, bytes(8))).can_id == 0x700


def test_energy_counters_that_look_like_a_pack_summary_are_still_counters():
    """The counters' low words can fall in the summary's ranges for a while."""

    layout = ShiftedIdLayout(active=True)
    summary = frame(0x150, _u16(534) + (120).to_bytes(2, "little", signed=True) + _u16(900) + _u16(1000))
    assert layout.normalise(summary).can_id == 0x150
    # 0x0002_0118 Wh charged reads as 28.0 V: out of range, so counters.
    charged, discharged = 0x0002_0118, 0x0000_0320
    first = frame(0x150, _u32(charged) + _u32(discharged), 10.0)
    assert layout.normalise(first).can_id == 0x550
    # 5.35 s later 40 Wh more reads as 32.0 V, still out of range ...
    second = frame(0x150, _u32(charged + 40) + _u32(discharged), 15.35)
    assert layout.normalise(second).can_id == 0x550
    # ... and 40 Wh after that as 36.0 V, SOC 80 %, SOH 0 %: in range, but it
    # continues the counters, so it is still read as counters.
    third = frame(0x150, _u32(charged + 80) + _u32(discharged), 20.7)
    assert layout.normalise(third).can_id == 0x550
    # A real summary between them is still a summary.
    assert layout.normalise(frame(0x150, summary.payload, 21.0)).can_id == 0x150
