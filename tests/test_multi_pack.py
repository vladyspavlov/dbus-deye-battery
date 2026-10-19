"""Parallel banks: several Deye packs linked by their parallel cable.

The ``0x110``-family frames repeat once per pack at ``base + (BmsAddr - 1)``;
the ``0x35x``/``0x36x``/``0x371`` frames describe the whole bank.  These tests
build a synthetic bank shaped like the two-pack capture in issue #6, where a
full pack 1 opened its charge MOS while pack 2 kept accepting 45 A.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.cli import REDACTED, redact_serial
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.decoder import MAX_PACKS, DeyeDecoder
from deye_virtual_battery.policy import evaluate_policy
from deye_virtual_battery.stateful import StatefulShadow
from deye_virtual_battery.venus_bms_publisher import (
    BATTERY_REQUEST_PATH,
    _accept_battery_request,
    _add_paths,
)
from deye_virtual_battery.policy import PolicyConfig


CHARGE = 0x10
DISCHARGE = 0x20
PARALLEL = 0x01


def _u16(value: float) -> bytes:
    return int(round(value)).to_bytes(2, "little", signed=False)


def _i16(value: float) -> bytes:
    return int(round(value)).to_bytes(2, "little", signed=True)


def _u32(value: float) -> bytes:
    return int(round(value)).to_bytes(4, "little", signed=False)


@dataclass
class Pack:
    charge_closed: bool = True
    discharge_closed: bool = True
    voltage: float = 53.4
    # Deye's own sign: positive is discharge.
    deye_current: float = 0.0
    soc: float = 90.0
    allowable_charge: int = 45
    allowable_discharge: int = 100
    cell_max: float = 3.343
    cell_min: float = 3.331
    temperature_max: float = 23.0
    fault_level: int = 0
    cycles: int = 11
    balance_mask: int = 0
    conditions: bytes = bytes(7)
    charged_wh: int = 45_000
    discharged_wh: int = 33_000
    serial: str | None = None


@dataclass
class Bank:
    packs: list[Pack] = field(default_factory=lambda: [Pack(), Pack()])
    cvl: float = 58.4
    ccl: float = 45.0
    dcl: float = 200.0
    array_ccl: float | None = None
    soc: int = 94
    capacity_ah: float = 200.0
    # Deye's own sign: positive is discharge.
    deye_current: float = -0.7
    charge_disabled: int | None = None
    discharge_disabled: int = 0
    parallel_connected: int | None = None
    # Packs whose frames are not on the wire (1-based).
    silent: tuple[int, ...] = ()

    def frames(self, timestamp: float) -> list[str]:
        lines: list[tuple[int, bytes]] = []
        count = len(self.packs)
        charge_disabled = (
            self.charge_disabled
            if self.charge_disabled is not None
            else sum(not pack.charge_closed for pack in self.packs)
        )
        connected = self.parallel_connected or count
        lines += [
            (0x351, _u16(self.cvl * 10) + _i16(self.ccl * 10) + _i16(self.dcl * 10) + _u16(480)),
            (0x355, _u16(self.soc) + _u16(100) + bytes(4)),
            (0x356, _i16(53.4 * 100) + _i16(self.deye_current * 10) + _i16(220) + bytes(2)),
            (0x359, bytes(8)),
            (0x35C, bytes([0x40]) + bytes(7)),
            (0x35E, b"DY001" + bytes([0x16]) + _u16(self.capacity_ah * 10)),
            (0x361, _u16(3343) + _u16(3331) + _i16(230) + _i16(220)),
            (0x364, bytes([count, charge_disabled, self.discharge_disabled, 0, connected, 0, 0, 0])),
            (
                0x371,
                _i16((self.array_ccl if self.array_ccl is not None else self.ccl) * 10)
                + _i16(self.dcl * 10)
                + bytes(4),
            ),
        ]
        for number, pack in enumerate(self.packs, 1):
            if number in self.silent:
                continue
            offset = number - 1
            flags = PARALLEL
            flags |= CHARGE if pack.charge_closed else 0
            flags |= DISCHARGE if pack.discharge_closed else 0
            lines += [
                (0x110 + offset, pack.conditions + bytes([flags])),
                (
                    0x150 + offset,
                    _u16(pack.voltage * 10) + _i16(pack.deye_current * 10)
                    + _u16(pack.soc * 10) + _u16(1000),
                ),
                (
                    0x200 + offset,
                    _u16(pack.cell_max * 1000) + _u16(pack.cell_min * 1000)
                    + _i16(pack.temperature_max * 10) + _i16(220),
                ),
                (
                    0x250 + offset,
                    _i16(250) + _i16(-400) + _u16(pack.allowable_charge)
                    + _u16(pack.allowable_discharge),
                ),
                (
                    0x400 + offset,
                    bytes([0, pack.fault_level]) + _u16(pack.cycles)
                    + _u16(pack.balance_mask) + _u16(3),
                ),
                (0x550 + offset, _u32(pack.charged_wh) + _u32(pack.discharged_wh)),
            ]
            if pack.serial is not None:
                lines += [
                    (0x600 + offset, pack.serial[:8].encode()),
                    (0x650 + offset, pack.serial[8:16].encode()),
                ]
        return [
            f"({timestamp + index * 0.001:.6f}) can0 {can_id:03X}#{payload.hex().upper()} R"
            for index, (can_id, payload) in enumerate(lines)
        ]

    def cache(self, seconds: int = 3, start: float = 1000.0) -> tuple[VirtualBatteryCache, float]:
        cache = VirtualBatteryCache()
        timestamp = start
        for second in range(seconds):
            timestamp = start + second
            for line in self.frames(timestamp):
                cache.apply(parse_candump_line(line))
        return cache, timestamp + 0.2

    def policy(self) -> dict:
        cache, at = self.cache()
        return evaluate_policy(cache.snapshot(at=at))

    def model(self) -> dict:
        cache, at = self.cache()
        return build_mock_dbus_model(cache.snapshot(at=at))


def issue_6_bank() -> Bank:
    return Bank(
        packs=[
            Pack(charge_closed=False, soc=97.6, allowable_charge=18, voltage=53.3),
            Pack(charge_closed=True, soc=90.5, allowable_charge=45, deye_current=-0.7),
        ],
        ccl=45.0,
    )


# -- decoding --------------------------------------------------------------


def test_second_pack_frames_decode_under_their_own_pack_number():
    decoder = DeyeDecoder()
    fields = decoder.decode(parse_candump_line("(1.0) can0 111#0000000000000031 R"))
    names = {item.name: item for item in fields}
    assert names["packs.2.mos.charge_closed"].value is True
    assert names["packs.2.mos.charge_closed"].source_can_id == 0x111
    # Pack 2 never overwrites pack 1's original fields.
    assert "mos.charge_closed" not in names


def test_pack_one_keeps_its_original_names_and_gains_a_pack_mirror():
    decoder = DeyeDecoder()
    fields = decoder.decode(parse_candump_line("(1.0) can0 110#0000000000000021 R"))
    names = {item.name: item.value for item in fields}
    assert names["mos.charge_closed"] is False
    assert names["packs.1.mos.charge_closed"] is False


def test_cell_extrema_of_a_second_pack_keep_the_base_layout_name():
    decoder = DeyeDecoder()
    fields = decoder.decode(parse_candump_line("(1.0) can0 201#0F0D0B0DE600E600 R"))
    names = {item.name: item.value for item in fields}
    assert names["packs.2.cells.max_voltage_200"] == 3.343
    assert "cells.max_voltage_201" not in names


def test_per_pack_ids_beyond_the_supported_count_and_tx_echoes_are_ignored():
    decoder = DeyeDecoder()
    beyond = 0x110 + MAX_PACKS
    assert decoder.decode(parse_candump_line(f"(1.0) can0 {beyond:03X}#0000000000000031 R")) == []
    assert decoder.decode(parse_candump_line("(1.0) can0 111#0000000000000031 T")) == []


def test_a_two_pack_capture_has_no_unrecognised_frames():
    cache, _ = issue_6_bank().cache()
    assert cache.unknown_frames == 0
    assert cache.decode_errors == 0


# -- charge permission and limits ------------------------------------------


def test_issue_6_a_full_pack_does_not_block_the_whole_bank():
    policy = issue_6_bank().policy()
    assert policy["permissions"]["charge_inhibit_reasons"] == []
    assert policy["effective_limits"]["ccl_a"] == 45.0
    assert policy["permissions"]["allow_charge"] is True
    # The normal ceiling, not the blocked-charge CVL.
    assert policy["effective_limits"]["cvl_v"] == 57.2
    assert policy["diagnostics"]["charge_mos_closed"] is True


def test_every_pack_blocking_charge_still_blocks_the_bank():
    bank = issue_6_bank()
    bank.packs[1].charge_closed = False
    policy = bank.policy()
    assert policy["effective_limits"]["ccl_a"] == 0.0
    assert policy["effective_limits"]["cvl_v"] == 55.2
    assert "charge_mos_open" in policy["permissions"]["charge_inhibit_reasons"]


def test_every_pack_blocking_discharge_still_blocks_discharge():
    bank = issue_6_bank()
    for pack in bank.packs:
        pack.discharge_closed = False
    bank.discharge_disabled = 2
    policy = bank.policy()
    assert policy["effective_limits"]["dcl_a"] == 0.0
    assert "discharge_mos_open" in policy["permissions"]["discharge_inhibit_reasons"]


def test_one_pack_blocking_discharge_leaves_the_bank_discharging():
    bank = issue_6_bank()
    bank.packs[0].discharge_closed = False
    bank.discharge_disabled = 1
    policy = bank.policy()
    assert policy["effective_limits"]["dcl_a"] == 200.0


def test_pack_ceiling_is_the_sum_of_packs_that_accept_charge():
    # Pack 1's own 45 A capped the old driver at 46 A for a 90 A bank.
    bank = Bank(packs=[Pack(allowable_charge=45), Pack(allowable_charge=45)], ccl=90.0)
    policy = bank.policy()
    assert policy["raw_limits"]["pack_allowable_ccl_a"] == 90.0
    assert policy["effective_limits"]["ccl_a"] == 90.0


def test_a_lower_pack_sum_still_caps_the_bank():
    bank = Bank(packs=[Pack(allowable_charge=20), Pack(allowable_charge=20)], ccl=90.0)
    policy = bank.policy()
    # 40 A plus one amp of quantization per contributing pack.
    assert policy["effective_limits"]["ccl_a"] == 42.0


def test_bank_ccl_is_never_raised_above_0x351():
    bank = Bank(packs=[Pack(allowable_charge=100), Pack(allowable_charge=100)], ccl=30.0)
    assert bank.policy()["effective_limits"]["ccl_a"] == 30.0


def test_a_silent_pack_neither_caps_nor_blocks_the_bank_on_its_own():
    # 0x364 says two packs, one blocking charge; only pack 1 (blocked) is
    # visible.  0x364 is the only word on the silent pack.
    bank = issue_6_bank()
    bank.silent = (2,)
    policy = bank.policy()
    assert policy["effective_limits"]["ccl_a"] == 45.0
    assert policy["raw_limits"]["pack_allowable_ccl_a"] is None


def test_a_pack_that_goes_silent_is_reported_offline():
    bank = issue_6_bank()
    cache, at = bank.cache()
    bank.silent = (2,)
    for line in bank.frames(at + 5.0):
        cache.apply(parse_candump_line(line))
    paths = build_mock_dbus_model(cache.snapshot(at=at + 5.2))["paths"]
    assert paths["/Diagnostics/Pack/2/Online"] == 0
    assert paths["/Diagnostics/Bank/Complete"] == 0
    assert paths["/Battery/2/Voltage"] is None
    assert cache.snapshot(at=at + 5.2)["health"]["stale_packs"] == [2]


def test_a_silent_pack_with_every_module_blocking_still_blocks():
    bank = issue_6_bank()
    bank.silent = (2,)
    bank.charge_disabled = 2
    assert bank.policy()["effective_limits"]["ccl_a"] == 0.0


def test_single_pack_behaviour_is_unchanged():
    bank = Bank(packs=[Pack(charge_closed=False, allowable_charge=18)], ccl=45.0, capacity_ah=100.0)
    policy = bank.policy()
    assert policy["effective_limits"]["ccl_a"] == 0.0
    assert policy["permissions"]["charge_inhibit_reasons"] == ["charge_mos_open"]
    bank.packs[0].charge_closed = True
    policy = bank.policy()
    # One pack keeps the single-pack ceiling: its own 18 A plus one.
    assert policy["effective_limits"]["ccl_a"] == 19.0


# -- alarms ----------------------------------------------------------------


def test_a_condition_on_the_second_pack_raises_the_alarm():
    bank = issue_6_bank()
    bank.packs[1].conditions = bytes([0x01, 0, 0, 0, 0, 0, 0])  # cell_over_voltage
    policy = bank.policy()
    assert policy["standard_alarms"]["/Alarms/HighCellVoltage"] == 2
    assert "cell_over_voltage" in policy["active_deye_conditions"]


def test_a_major_fault_on_the_second_pack_raises_internal_failure():
    bank = issue_6_bank()
    bank.packs[1].fault_level = 2
    policy = bank.policy()
    assert policy["standard_alarms"]["/Alarms/InternalFailure"] == 2
    assert policy["diagnostics"]["system_fault_level"] == "major"


# -- current sign cross-check ----------------------------------------------


def test_packs_carrying_opposite_currents_are_not_a_sign_conflict():
    # Pack 2 (fuller) feeds pack 1 at 5 A while the bank is net charging 2 A.
    bank = Bank(
        packs=[Pack(deye_current=-7.0), Pack(deye_current=5.0)],
        deye_current=-2.0,
    )
    cache, at = bank.cache(seconds=20)
    assert cache.sign_convention_conflict is False
    assert cache.sign_crosscheck_disagreements == 0
    model = build_mock_dbus_model(cache.snapshot(at=at))
    assert model["paths"]["/Dc/0/Current"] == 2.0
    assert model["paths"]["/Diagnostics/Alarms/CurrentSignConflict"] == 0


def test_a_real_sign_inversion_is_still_caught_with_two_packs():
    bank = Bank(
        packs=[Pack(deye_current=-3.0), Pack(deye_current=-3.0)],
        # The bank claims discharge while both packs charge.
        deye_current=6.0,
    )
    cache, _ = bank.cache(seconds=20)
    assert cache.sign_convention_conflict is True


# -- what Venus and the GUI see --------------------------------------------


def test_the_gui_sees_two_batteries_in_parallel():
    paths = issue_6_bank().model()["paths"]
    assert paths["/System/NrOfBatteries"] == 2
    assert paths["/System/BatteriesParallel"] == 2
    assert paths["/System/BatteriesSeries"] == 1
    assert paths["/System/NrOfCellsPerBattery"] == 16
    assert paths["/System/NrOfModulesOnline"] == 2
    assert paths["/System/NrOfModulesBlockingCharge"] == 1


def test_capacity_is_the_bank_capacity_and_is_not_summed_again():
    # 0x35E already reports two 100 Ah packs as 200 Ah.
    paths = issue_6_bank().model()["paths"]
    assert paths["/InstalledCapacity"] == 200.0
    assert paths["/Capacity"] == 200.0


def test_each_pack_is_published_on_its_own():
    paths = issue_6_bank().model()["paths"]
    assert paths["/Battery/1/Soc"] == 97.6
    assert paths["/Battery/2/Soc"] == 90.5
    assert paths["/Battery/2/Voltage"] == 53.4
    assert paths["/Battery/2/Current"] == 0.7
    assert paths["/Battery/2/Temperature"] == 23.0
    assert paths["/Diagnostics/Pack/1/ChargeMosClosed"] == 0
    assert paths["/Diagnostics/Pack/2/ChargeMosClosed"] == 1
    assert paths["/Diagnostics/Pack/1/AllowableChargeCurrent"] == 18.0
    assert paths["/Diagnostics/Pack/2/Online"] == 1
    assert paths["/Diagnostics/Pack/3/Online"] is None
    assert paths["/Battery/3/Voltage"] is None
    assert paths[f"/Battery/{MAX_PACKS}/Voltage"] is None
    assert f"/Battery/{MAX_PACKS + 1}/Voltage" not in paths


def test_history_covers_every_pack():
    bank = issue_6_bank()
    bank.packs[0].charged_wh, bank.packs[1].charged_wh = 48_727, 45_116
    bank.packs[0].cycles, bank.packs[1].cycles = 11, 14
    paths = bank.model()["paths"]
    assert paths["/History/ChargedEnergy"] == 93.843
    assert paths["/History/ChargeCycles"] == 14


def test_balancing_on_any_pack_is_reported():
    bank = issue_6_bank()
    assert bank.model()["paths"]["/Balancing"] == 0
    bank.packs[1].balance_mask = 0x0004
    assert bank.model()["paths"]["/Balancing"] == 1


def test_one_pack_reports_one_battery():
    bank = Bank(packs=[Pack()], capacity_ah=100.0)
    paths = bank.model()["paths"]
    assert paths["/System/NrOfBatteries"] == 1
    assert paths["/System/BatteriesParallel"] == 1
    assert paths["/Battery/1/Voltage"] == 53.4
    assert paths["/Battery/2/Voltage"] is None


def test_the_path_set_does_not_depend_on_how_many_packs_are_present():
    """The publisher registers paths once and must find each one later."""
    one = set(Bank(packs=[Pack()]).model()["paths"])
    two = set(issue_6_bank().model()["paths"])
    assert one == two


def test_pack_paths_are_invalidated_when_the_service_loses_can():
    bank = issue_6_bank()
    shadow = StatefulShadow(policy_config=PolicyConfig())
    cache = VirtualBatteryCache()
    for second in range(6):
        at = 1000.0 + second
        for line in bank.frames(at):
            cache.apply(parse_candump_line(line))
        online = shadow.step(cache.snapshot(at=at + 0.2), timestamp=at + 0.2, vebus_voltage_v=None)
    assert online["lifecycle"]["state"] == "online"
    assert online["paths"]["/Battery/2/Voltage"] == 53.4
    later = at + 30.0
    lost = shadow.step(cache.snapshot(at=later), timestamp=later, vebus_voltage_v=None)
    assert lost["paths"]["/Battery/2/Voltage"] is None
    assert lost["paths"]["/Balancing"] is None


# -- publisher and privacy -------------------------------------------------


def test_the_gui_pack_selector_is_writable_and_bounded():
    assert _accept_battery_request(BATTERY_REQUEST_PATH, 2) is True
    assert _accept_battery_request(BATTERY_REQUEST_PATH, MAX_PACKS) is True
    for refused in (0, MAX_PACKS + 1, True, "2", 1.5, None):
        assert _accept_battery_request(BATTERY_REQUEST_PATH, refused) is False


def test_the_pack_selector_is_registered_writable_and_never_rewritten():
    registered: dict[str, dict] = {}

    class FakeService:
        def add_path(self, path, value, **options):
            registered[path] = {"value": value, **options}

    model = issue_6_bank().model()
    dynamic = _add_paths(FakeService(), model["paths"], config=PolicyConfig())
    selector = registered[BATTERY_REQUEST_PATH]
    assert selector["value"] == 1
    assert selector["writeable"] is True
    assert selector["onchangecallback"] is _accept_battery_request
    assert BATTERY_REQUEST_PATH not in dynamic
    assert "/Battery/2/Voltage" in dynamic


@pytest.mark.parametrize("serial", ["DEYEPACKTWO00001"])
def test_per_pack_serials_are_redacted_from_offline_output(serial):
    bank = issue_6_bank()
    bank.packs[1].serial = serial
    cache, at = bank.cache()
    snapshot = cache.snapshot(at=at)
    model = build_mock_dbus_model(snapshot)
    assert model["paths"]["/Battery/2/Serial"] == serial
    redact_serial(snapshot)
    redact_serial(model)
    assert model["paths"]["/Battery/2/Serial"] == REDACTED
    assert serial[:8] not in repr(snapshot)
    assert serial[:8] not in repr(model)
