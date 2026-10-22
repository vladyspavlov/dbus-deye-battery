"""Undo the id shift of Deye firmware that clears bit 10 of its CAN ids.

Some Deye firmware sends every per-pack frame whose id is ``0x400`` or above
with bit 10 cleared (issue #7, a two-pack SE-F5 Pro-C bank whose master
reports software word ``0x02E0``).  The frames are otherwise exactly the V3.3
ones, at the V3.3 rates:

==========  =============  ====================================
documented  arrives as     collides with
==========  =============  ====================================
``0x400``   ``0x000``      nothing
``0x500``   ``0x100``      nothing (``0x100`` predates V2.1)
``0x550``   ``0x150``      each pack's own ``0x150``
``0x600``   ``0x200``      each pack's own ``0x200``
``0x650``   ``0x250``      each pack's own ``0x250``
``0x700``   ``0x300``      ``0x305``/``0x307`` from pack 6 up
``0x750``   ``0x350``      the bank's ``0x351`` from pack 2 up
==========  =============  ====================================

Pack 2's ``0x751`` arriving as ``0x351`` was the all-zero limit frame of
issue #7; the decoder refuses it on its own impossible voltages.

The same model with other firmware sends the documented ids, so nothing here
acts until the bank has proven that it shifts: two frames whose payload only
a shifted frame can carry, a software-version frame (``0xAA`` marker and an
ASCII boot version) on ``0x100`` or an eight-character serial half on
``0x200``/``0x250``.  A real cell-extremes or temperature frame can never be
printable text.  A battery frame on a documented id of ``0x400`` or above
proves the opposite, and then nothing is ever remapped.

Once proven, the shift is assumed for the life of the process.  Remapping
feeds the critical ``0x400`` fields, and dropping it again would let them go
stale and take the battery offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .candump import CanFrame
from .decoder import MAX_PACKS, PER_PACK_BASE_IDS


#: Frames proving the shift before anything is remapped.
EVIDENCE_REQUIRED = 2

#: The fastest the energy counters of one pack can plausibly grow, in Wh per
#: second (30 kW), plus a fixed allowance for the counters' own rounding.
ENERGY_RATE_WH_PER_S = 30_000 / 3600
ENERGY_ALLOWANCE_WH = 10

SHIFT = 0x400
#: The PCS's own frames; never treated as shifted battery frames.
INVERTER_IDS = frozenset({0x305, 0x306, 0x307})
#: Documented per-pack families at 0x400 and above.
HIGH_BASES = tuple(base for base in PER_PACK_BASE_IDS if base >= SHIFT)
#: 0x750 + n - 1 below the shift is 0x35x; only these slots carry no bank frame.
SHIFTED_750_IDS = frozenset({0x350, 0x352, 0x353, 0x354})


def _printable(data: bytes) -> bool:
    return all(32 <= byte < 127 for byte in data)


def _version_frame(payload: bytes) -> bool:
    return len(payload) == 8 and payload[2] == 0xAA and _printable(payload[3:8])


def _text_frame(payload: bytes) -> bool:
    return len(payload) == 8 and _printable(payload)


def _family(can_id: int, base: int, count: int = MAX_PACKS) -> bool:
    return base <= can_id < base + count


@dataclass(slots=True)
class ShiftedIdLayout:
    """Detect the bit-10 shift and map shifted frames back to their ids."""

    active: bool = False
    #: A documented id at 0x400 or above has been seen; never remap.
    documented: bool = False
    evidence: int = 0
    remapped_frames: int = 0
    _energy: dict[int, tuple[int, int, float]] = field(default_factory=dict)

    def normalise(self, frame: CanFrame) -> CanFrame:
        """Return the frame under the id the protocol documents for it."""

        if frame.direction == "T" or frame.dlc != 8:
            return frame
        can_id = frame.can_id
        if not self.documented and any(_family(can_id, base) for base in HIGH_BASES):
            self.documented = True
            self.active = False
        if self.documented:
            return frame
        if not self.active:
            self._observe(can_id, frame.payload)
            if not self.active:
                return frame
        target = self._target(frame)
        if target is None:
            return frame
        self.remapped_frames += 1
        return replace(frame, can_id=target)

    def _observe(self, can_id: int, payload: bytes) -> None:
        if (_family(can_id, 0x100, 16) and _version_frame(payload)) or (
            (_family(can_id, 0x200) or _family(can_id, 0x250)) and _text_frame(payload)
        ):
            self.evidence += 1
            if self.evidence >= EVIDENCE_REQUIRED:
                self.active = True

    def _target(self, frame: CanFrame) -> int | None:
        can_id, payload = frame.can_id, frame.payload
        if _family(can_id, 0x000):
            return can_id + SHIFT
        if _family(can_id, 0x100, 16):
            return can_id + SHIFT if _version_frame(payload) else None
        if _family(can_id, 0x150):
            return can_id + SHIFT if self._energy_frame(frame) else None
        if _family(can_id, 0x200) or _family(can_id, 0x250):
            return can_id + SHIFT if _text_frame(payload) else None
        if _family(can_id, 0x300) and can_id not in INVERTER_IDS:
            return can_id + SHIFT
        if can_id in SHIFTED_750_IDS:
            return can_id + SHIFT
        return None

    def _energy_frame(self, frame: CanFrame) -> bool:
        """Tell a pack's 0x550 energy counters from its own 0x150 summary.

        A summary carries a pack voltage, SOC and SOH in range; the counters
        almost never do, and when they happen to, they still continue the
        pack's previous counters, which a summary cannot.
        """

        payload = frame.payload
        charged = int.from_bytes(payload[0:4], "little")
        discharged = int.from_bytes(payload[4:8], "little")
        voltage = int.from_bytes(payload[0:2], "little")
        soc = int.from_bytes(payload[4:6], "little")
        soh = int.from_bytes(payload[6:8], "little")
        # The decoder's own ranges, in wire units: 35-65 V, 0-100 %.
        summary = 350 <= voltage <= 650 and soc <= 1000 and soh <= 1000
        previous = self._energy.get(frame.can_id)
        continues = False
        if previous is not None:
            last_charged, last_discharged, last_timestamp = previous
            allowance = ENERGY_ALLOWANCE_WH + ENERGY_RATE_WH_PER_S * max(
                0.0, frame.timestamp - last_timestamp
            )
            continues = (
                0 <= charged - last_charged <= allowance
                and 0 <= discharged - last_discharged <= allowance
            )
        if summary and not continues:
            return False
        self._energy[frame.can_id] = (charged, discharged, frame.timestamp)
        return True
