"""Bank-level view of a Deye parallel group.

Deye packs linked by their parallel cable present one bank on the PCS port.
The ``0x35x``/``0x36x``/``0x371`` frames already describe that bank: limits,
SOC, capacity and the module counts are the master's summary of every pack.
The ``0x110``-family frames do not; each one describes a single pack, and the
pack number is in the CAN id (see :data:`decoder.PER_PACK_BASE_IDS`).

Treating pack 1's frames as the bank was the cause of issue #6: with two
packs, a full pack 1 opened its charge MOS while pack 2 still accepted 45 A,
the bank asked for 45 A in ``0x351``, and the driver published 0 A.

Everything here is derived from the per-pack ``packs.<n>.`` fields.  With a
single pack every value equals what pack 1 reports, so a one-pack system
behaves exactly as it did before multi-pack support.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from .decoder import pack_field_name


PACK_FIELD = re.compile(r"^packs\.(\d+)\.")
FAULT_RANK = {"none": 0, "minor": 1, "major": 2}

Lookup = Callable[[str], Any]


def pack_numbers(names: Iterable[str]) -> list[int]:
    """Every pack that has ever been seen on the wire."""

    numbers = set()
    for name in names:
        # Same test as PACK_FIELD, without a regex call for every field.
        if name.startswith("packs."):
            number, dot, _ = name[6:].partition(".")
            if dot and number.isdigit():
                numbers.add(int(number))
    return sorted(numbers)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _any_closed(states: list[Any]) -> bool | None:
    known = [state for state in states if isinstance(state, bool)]
    if not known:
        return None
    return any(known)


class BankView:
    """Aggregate the per-pack fields one lookup function can see."""

    def __init__(self, names: Iterable[str], effective: Lookup) -> None:
        self.effective = effective
        self.seen = pack_numbers(names)
        # A pack is online while its 0x110-family frame is fresh: that frame
        # carries the MOS state every charge decision depends on.
        self.online = [
            pack for pack in self.seen if self.pack(pack, "mos.raw_flags") is not None
        ]
        connected = _number(effective("modules.parallel_connected"))
        if connected is not None and connected > 0:
            # 0x364 is the master's own count, so it also covers a pack whose
            # frames this driver has not received yet.
            self.expected = max(int(connected), len(self.online))
        else:
            # The victronCAN profile does not send 0x364.
            self.expected = len(self.seen)
        self.complete = bool(self.seen) and len(self.online) >= self.expected

    def pack(self, pack: int, name: str) -> Any:
        return self.effective(pack_field_name(pack, name))

    @property
    def multiple(self) -> bool:
        return self.expected > 1 or len(self.seen) > 1

    def _blocking_count_allows(self, name: str) -> bool:
        """Whether 0x364 says some pack this driver cannot see is not blocking.

        Only consulted when a pack is missing: 0x364 alone is not trusted as
        permission, but it is the only word on a pack whose frames are absent.
        """

        if self.complete:
            return False
        blocking = _number(self.effective(name))
        return blocking is not None and blocking < self.expected

    def charge_mos_closed(self) -> bool | None:
        states = [self.pack(pack, "mos.charge_closed") for pack in self.online]
        closed = _any_closed(states)
        if not self.multiple:
            return closed
        if closed is not True and self._blocking_count_allows("modules.charge_disabled"):
            return True
        return closed

    def discharge_mos_closed(self) -> bool | None:
        states = [self.pack(pack, "mos.discharge_closed") for pack in self.online]
        closed = _any_closed(states)
        if not self.multiple:
            return closed
        if closed is not True and self._blocking_count_allows("modules.discharge_disabled"):
            return True
        return closed

    def allowable_charge_current(self) -> tuple[float | None, float | None]:
        """Return the 0x250-family charge ceiling as ``(value, ceiling)``.

        One pack: its own value, as before.  Several: the sum over packs whose
        charge MOS is closed, which is how the master builds the bank CCL (in
        the issue #6 capture pack 1 offered 18 A with its MOS open, pack 2
        45 A, and 0x351 said 45 A).  Each pack quantizes to whole amps, so the
        ceiling allows one amp per contributing pack.  An incomplete bank
        yields no value: the cross-check is a second opinion and must not cap
        the bank on a partial sum.
        """

        if not self.multiple:
            value = _number(self.pack(1, "pack.maximum_allowable_charge_current"))
            return value, (value + 1.0 if value is not None else None)
        if not self.complete:
            return None, None
        contributing = [
            pack for pack in self.online if self.pack(pack, "mos.charge_closed") is True
        ]
        if not contributing:
            return None, None
        values = [
            _number(self.pack(pack, "pack.maximum_allowable_charge_current"))
            for pack in contributing
        ]
        if any(value is None for value in values):
            return None, None
        total = round(sum(values), 1)  # type: ignore[arg-type]
        return total, total + float(len(contributing))

    def active_conditions(self) -> list[str]:
        active: set[str] = set()
        for pack in self.online:
            value = self.pack(pack, "pack.active_v33_conditions")
            if isinstance(value, list):
                active.update(item for item in value if isinstance(item, str))
        return sorted(active)

    def fault_level(self) -> str | None:
        levels = [self.pack(pack, "system.fault_level") for pack in self.online]
        ranked = [level for level in levels if level in FAULT_RANK]
        if not ranked:
            return None
        return max(ranked, key=FAULT_RANK.__getitem__)

    def _sum(self, name: str, digits: int) -> float | None:
        if not self.multiple:
            return _number(self.pack(1, name))
        if not self.complete:
            return None
        values = [_number(self.pack(pack, name)) for pack in self.online]
        if any(value is None for value in values):
            return None
        return round(sum(values), digits)  # type: ignore[arg-type]

    def charged_energy(self) -> float | None:
        return self._sum("history.charged_energy", 3)

    def discharged_energy(self) -> float | None:
        return self._sum("history.discharged_energy", 3)

    def current_victron(self) -> float | None:
        """Sum of the packs' own currents, Victron sign convention."""

        return self._sum("diagnostics.current_victron_150", 1)

    def cycle_count(self) -> float | None:
        values = [_number(self.pack(pack, "battery.cycle_count")) for pack in self.seen]
        present = [value for value in values if value is not None]
        # The most-cycled pack is the honest figure for the bank's wear.
        return max(present) if present else None

    def balancing(self) -> bool | None:
        masks = [_number(self.pack(pack, "cells.balance_mask")) for pack in self.online]
        known = [mask for mask in masks if mask is not None]
        if not known:
            return None
        return any(mask > 0 for mask in known)

    def values(self) -> dict[str, Any]:
        allowable, ceiling = self.allowable_charge_current()
        return {
            "bank.pack_count": self.expected if self.seen else None,
            "bank.packs_seen": list(self.seen),
            "bank.packs_online": list(self.online),
            "bank.packs_missing": [
                pack for pack in self.seen if pack not in self.online
            ],
            "bank.complete": self.complete,
            "bank.charge_mos_closed": self.charge_mos_closed(),
            "bank.discharge_mos_closed": self.discharge_mos_closed(),
            "bank.pack_allowable_ccl": allowable,
            "bank.pack_allowable_ccl_ceiling": ceiling,
            "bank.active_pack_conditions": self.active_conditions(),
            "bank.fault_level": self.fault_level(),
            "bank.charged_energy": self.charged_energy(),
            "bank.discharged_energy": self.discharged_energy(),
            "bank.cycle_count": self.cycle_count(),
            "bank.balancing": self.balancing(),
            "bank.current_victron": self.current_victron(),
        }


def bank_snapshot_fields(fields: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Derive ``bank.*`` entries shaped like the cache's field snapshots."""

    view = BankView(fields, lambda name: _effective(fields, name))
    if not view.seen:
        return {}
    return {name: _derived(value) for name, value in view.values().items()}


def _effective(fields: dict[str, dict[str, Any]], name: str) -> Any:
    state = fields.get(name)
    return state.get("effective_value") if isinstance(state, dict) else None


def _derived(value: Any) -> dict[str, Any]:
    usable = value is not None
    return {
        "cached_value": value,
        "effective_value": value,
        "unit": None,
        "category": "derived",
        "source_can_id": "derived",
        "last_valid_timestamp": None,
        "last_valid_utc": None,
        "age_seconds": None,
        "freshness": "derived" if usable else "invalid",
        "usable": usable,
        "confidence": "derived",
        "note": "Aggregated from the per-pack packs.<n>. fields",
        "updates": 0,
        "rejected_updates": 0,
        "last_rejected_timestamp": None,
        "last_rejected_value": None,
    }
