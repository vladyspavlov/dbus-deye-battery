"""Detect which BMS-side CAN protocol profile the Deye battery is sending.

The SE-F12-C exposes a selectable inverter protocol.  Two profiles have been
observed on this installation:

``deye_native``
    The Sol-ark/Deye PCS V3.3 family.  It transmits the vendor frames
    ``0x359``, ``0x35C``, ``0x361``, ``0x363``, ``0x364`` and ``0x371``, and
    ``0x35E`` starts with ``DY``.  ``0x356`` byte 2-3 uses the Deye current
    sign, where a positive value means discharge.

``victron_can``
    A Victron/Pylontech-shaped profile.  It adds Victron's standard ``0x35A``
    warning/alarm frame and the ``0x35F`` battery-type frame, changes ``0x35E``
    to ``PYLON``, drops every Deye vendor frame listed above, and inverts
    ``0x356`` byte 2-3 so a positive value means charge, which is the sign
    convention Venus expects.

Both profiles keep the extended Deye diagnostic family (``0x110``, ``0x150``,
``0x200``, ``0x250``, ``0x400``, ``0x500``, ``0x550``, ``0x6xx``, ``0x7xx``),
so pack conditions, MOS state and cell extrema stay available in either mode.

This module only classifies observed frames.  It transmits nothing, contacts no
host, and makes no control decision.  Evidence and timing come from the
2026-09-01 18:33:29-18:33:58 UTC live profile switch preserved under
``captures/victroncan/2026-09-01/``; see
``docs/deye-victroncan-profile.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEYE_NATIVE = "deye_native"
VICTRON_CAN = "victron_can"
UNKNOWN = "unknown"

#: Frames only the Deye native profile transmits.
DEYE_EXCLUSIVE_IDS = frozenset({0x359, 0x35C, 0x361, 0x363, 0x364, 0x371})

#: Frames only the victronCAN profile transmits.
VICTRON_EXCLUSIVE_IDS = frozenset({0x35A, 0x35F})

#: Frames both profiles transmit, so they never contribute profile evidence.
PROFILE_NEUTRAL_IDS = frozenset(
    {0x110, 0x150, 0x200, 0x250, 0x351, 0x355, 0x356, 0x358, 0x400,
     0x500, 0x550, 0x600, 0x650, 0x700, 0x750}
)

#: ``0x35E`` manufacturer-name prefixes observed for each profile.
DEYE_IDENTITY_PREFIX = b"DY"
VICTRON_IDENTITY_PREFIX = b"PYLON"

#: The Deye 0x356 current sign is inverted for Victron's convention; the
#: victronCAN profile already sends Victron's convention.
CURRENT_SIGN_FACTORS = {DEYE_NATIVE: -1.0, VICTRON_CAN: 1.0}


def is_profile_evidence(can_id: int) -> bool:
    """True when a frame identifier can distinguish the two profiles."""

    return (
        can_id in DEYE_EXCLUSIVE_IDS
        or can_id in VICTRON_EXCLUSIVE_IDS
        or can_id == 0x35E
    )


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Timing values for profile acquisition and switching.

    ``evidence_timeout_seconds`` matches the three-second control deadline used
    everywhere else in this adapter.  ``switch_quiet_seconds`` is shorter than
    the fastest observed profile-marker period (about one second) but longer
    than observed frame jitter, so a single corrupted identifier cannot move
    the profile while the current profile is still transmitting.
    """

    evidence_timeout_seconds: float = 3.0
    switch_quiet_seconds: float = 0.75
    switch_evidence_count: int = 2
    hold_last_profile_seconds: float = 10.0
    transition_settle_seconds: float = 5.0


@dataclass(slots=True)
class _Evidence:
    last_timestamp: float | None = None
    recent: list[float] = field(default_factory=list)
    frames: int = 0

    def observe(self, timestamp: float) -> None:
        self.last_timestamp = timestamp
        self.frames += 1
        self.recent.append(timestamp)
        del self.recent[:-8]

    def count_since(self, since: float) -> int:
        return sum(1 for timestamp in self.recent if timestamp >= since)

    def age(self, timestamp: float) -> float | None:
        if self.last_timestamp is None:
            return None
        return max(0.0, timestamp - self.last_timestamp)


@dataclass(slots=True)
class ProtocolProfileDetector:
    """Classify the active BMS-side profile from passively observed frames."""

    config: ProfileConfig = field(default_factory=ProfileConfig)
    profile: str = UNKNOWN
    profile_since: float | None = None
    last_timestamp: float | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    _deye: _Evidence = field(default_factory=_Evidence)
    _victron: _Evidence = field(default_factory=_Evidence)

    def observe(self, can_id: int, payload: bytes, timestamp: float) -> None:
        """Record profile evidence for one received frame."""

        self.last_timestamp = (
            timestamp
            if self.last_timestamp is None
            else max(self.last_timestamp, timestamp)
        )
        if can_id in DEYE_EXCLUSIVE_IDS:
            self._deye.observe(timestamp)
        elif can_id in VICTRON_EXCLUSIVE_IDS:
            self._victron.observe(timestamp)
        elif can_id == 0x35E:
            if payload.startswith(VICTRON_IDENTITY_PREFIX):
                self._victron.observe(timestamp)
            elif payload.startswith(DEYE_IDENTITY_PREFIX):
                self._deye.observe(timestamp)
        else:
            return
        self._resolve(timestamp)

    def resolve(self, timestamp: float) -> str:
        """Return the active profile, expiring evidence that has gone stale."""

        self._resolve(timestamp)
        return self.profile

    def _resolve(self, timestamp: float) -> None:
        deye_age = self._deye.age(timestamp)
        victron_age = self._victron.age(timestamp)
        deye_fresh = (
            deye_age is not None and deye_age <= self.config.evidence_timeout_seconds
        )
        victron_fresh = (
            victron_age is not None
            and victron_age <= self.config.evidence_timeout_seconds
        )

        if deye_fresh and not victron_fresh:
            candidate = DEYE_NATIVE
        elif victron_fresh and not deye_fresh:
            candidate = VICTRON_CAN
        elif deye_fresh and victron_fresh:
            # Both families are within the freshness window.  This happens only
            # while a switch is in progress, so keep the established profile
            # until the outgoing family has been quiet long enough.
            candidate = self._overlap_candidate(timestamp, deye_age, victron_age)
        else:
            candidate = self._expired_candidate(timestamp)

        if candidate == self.profile:
            return
        previous = self.profile
        self.profile = candidate
        self.profile_since = timestamp
        self.transitions.append(
            {
                "timestamp": timestamp,
                "event": "protocol_profile_transition",
                "previous_profile": previous,
                "new_profile": candidate,
                "deye_evidence_age_seconds": deye_age,
                "victron_evidence_age_seconds": victron_age,
                "action_taken": False,
            }
        )
        del self.transitions[:-200]

    def _overlap_candidate(
        self, timestamp: float, deye_age: float | None, victron_age: float | None
    ) -> str:
        if self.profile == UNKNOWN:
            # First acquisition with contradictory evidence: prefer whichever
            # family spoke most recently rather than guessing a sign.
            if deye_age is None:
                return VICTRON_CAN
            if victron_age is None:
                return DEYE_NATIVE
            return VICTRON_CAN if victron_age < deye_age else DEYE_NATIVE
        incoming = VICTRON_CAN if self.profile == DEYE_NATIVE else DEYE_NATIVE
        outgoing_age = deye_age if incoming == VICTRON_CAN else victron_age
        incoming_evidence = self._victron if incoming == VICTRON_CAN else self._deye
        window_start = timestamp - self.config.evidence_timeout_seconds
        if (
            outgoing_age is not None
            and outgoing_age >= self.config.switch_quiet_seconds
            and incoming_evidence.count_since(window_start)
            >= self.config.switch_evidence_count
        ):
            return incoming
        return self.profile

    def _expired_candidate(self, timestamp: float) -> str:
        if self.profile == UNKNOWN or self.profile_since is None:
            return UNKNOWN
        newest = max(
            value
            for value in (self._deye.last_timestamp, self._victron.last_timestamp)
            if value is not None
        )
        if timestamp - newest <= self.config.hold_last_profile_seconds:
            # Brief silence on a bus that is otherwise alive should not discard
            # a known profile; freshness of the individual fields already
            # invalidates control data after three seconds.
            return self.profile
        return UNKNOWN

    def current_sign_factor(self, timestamp: float) -> float | None:
        """Return the 0x356 sign multiplier, or ``None`` while unresolved."""

        return CURRENT_SIGN_FACTORS.get(self.resolve(timestamp))

    def in_transition(self, timestamp: float) -> bool:
        """True shortly after a profile change, when signals are mixed."""

        if self.profile_since is None:
            return False
        return timestamp - self.profile_since < self.config.transition_settle_seconds

    def snapshot(self, timestamp: float) -> dict[str, Any]:
        profile = self.resolve(timestamp)
        return {
            "profile": profile,
            "profile_since": self.profile_since,
            "profile_age_seconds": (
                round(timestamp - self.profile_since, 6)
                if self.profile_since is not None
                else None
            ),
            "in_transition": self.in_transition(timestamp),
            "current_sign_factor": CURRENT_SIGN_FACTORS.get(profile),
            "deye_evidence_age_seconds": _rounded(self._deye.age(timestamp)),
            "victron_evidence_age_seconds": _rounded(self._victron.age(timestamp)),
            "deye_evidence_frames": self._deye.frames,
            "victron_evidence_frames": self._victron.frames,
            "transitions": list(self.transitions),
        }


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
