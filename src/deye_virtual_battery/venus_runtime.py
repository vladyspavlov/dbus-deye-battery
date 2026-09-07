"""Runtime-neutral helpers for a staged Venus D-Bus publisher.

This module contains no D-Bus calls and never opens or transmits on CAN.  The
live adapter is kept separate so framing, qualification and the published
model can be tested on a development PC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
import time
from typing import Any, Callable

from .cache import VirtualBatteryCache
from .candump import CanFrame
from .policy import PolicyConfig
from .stateful import StatefulShadow


CAN_FRAME_FORMAT = "=IB3x8s"
CAN_FRAME_SIZE = struct.calcsize(CAN_FRAME_FORMAT)
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_SFF_MASK = 0x000007FF


class SocketCanFrameError(ValueError):
    """Raised when a raw SocketCAN frame is unsafe or unsupported."""


@dataclass(slots=True)
class RuntimeClock:
    """Provide progressing timestamps that ignore backward wall-clock steps.

    Venus devices can start before network time is available and then move the
    realtime clock backward when NTP synchronizes.  Cache freshness and alarm
    debounce require a progressing clock, while epoch-shaped timestamps remain
    useful in diagnostics.  Anchor an epoch value to ``monotonic()`` and adopt
    realtime corrections only when they move time forward.
    """

    wall_time: Callable[[], float] = time.time
    monotonic_time: Callable[[], float] = time.monotonic
    _anchor_wall: float = field(init=False)
    _anchor_monotonic: float = field(init=False)
    _last_timestamp: float = field(init=False)

    def __post_init__(self) -> None:
        self._anchor_wall = float(self.wall_time())
        self._anchor_monotonic = float(self.monotonic_time())
        self._last_timestamp = self._anchor_wall

    def now(self) -> float:
        monotonic_now = float(self.monotonic_time())
        elapsed = max(0.0, monotonic_now - self._anchor_monotonic)
        candidate = self._anchor_wall + elapsed
        wall_now = float(self.wall_time())
        if wall_now > candidate:
            self._anchor_wall = wall_now
            self._anchor_monotonic = monotonic_now
            candidate = wall_now
        self._last_timestamp = max(self._last_timestamp, candidate)
        return self._last_timestamp


def decode_socketcan_frame(raw: bytes, *, timestamp: float | None = None) -> CanFrame:
    """Convert a Linux classical ``struct can_frame`` to the common model.

    Only standard, non-RTR, non-error frames are accepted because the observed
    Deye PCS protocol uses 11-bit data frames.  Direction is intentionally
    unknown: a passive raw socket can see both battery and stock-driver frames.
    """

    if len(raw) != CAN_FRAME_SIZE:
        raise SocketCanFrameError(
            f"expected {CAN_FRAME_SIZE}-byte classical CAN frame, received {len(raw)}"
        )
    raw_can_id, dlc, payload = struct.unpack(CAN_FRAME_FORMAT, raw)
    if raw_can_id & CAN_ERR_FLAG:
        raise SocketCanFrameError("CAN error frame is not battery telemetry")
    if raw_can_id & CAN_RTR_FLAG:
        raise SocketCanFrameError("CAN RTR frame is not battery telemetry")
    if raw_can_id & CAN_EFF_FLAG:
        raise SocketCanFrameError("extended CAN identifiers are not used by Deye PCS")
    if dlc > 8:
        raise SocketCanFrameError(f"classical CAN DLC exceeds eight bytes: {dlc}")
    can_id = raw_can_id & CAN_SFF_MASK
    observed_at = time.time() if timestamp is None else float(timestamp)
    data = payload[:dlc]
    return CanFrame(
        timestamp=observed_at,
        interface="can0",
        can_id=can_id,
        payload=data,
        direction=None,
        original=f"socketcan:{can_id:03X}#{data.hex().upper()}",
    )


def encode_socketcan_frame(can_id: int, payload: bytes) -> bytes:
    """Build a classical 11-bit SocketCAN frame for an explicitly approved TX."""

    if not 0 <= can_id <= CAN_SFF_MASK:
        raise SocketCanFrameError(f"standard CAN identifier out of range: {can_id}")
    if len(payload) > 8:
        raise SocketCanFrameError(f"classical CAN payload exceeds eight bytes: {len(payload)}")
    return struct.pack(
        CAN_FRAME_FORMAT,
        can_id,
        len(payload),
        payload.ljust(8, b"\0"),
    )


@dataclass(slots=True)
class VenusPublisherCore:
    """Feed passive CAN frames into the stateful virtual-battery policy."""

    policy_config: PolicyConfig = field(default_factory=PolicyConfig)
    cache: VirtualBatteryCache = field(default_factory=VirtualBatteryCache)
    shadow: StatefulShadow = field(init=False)
    last_model: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.shadow = StatefulShadow(policy_config=self.policy_config)

    def apply_raw_frame(self, raw: bytes, *, timestamp: float | None = None) -> CanFrame:
        frame = decode_socketcan_frame(raw, timestamp=timestamp)
        self.cache.apply(frame)
        return frame

    def step(
        self,
        *,
        timestamp: float,
        vebus_voltage_v: float | None,
    ) -> dict[str, Any]:
        self.last_model = self.shadow.step(
            self.cache.snapshot(at=timestamp),
            timestamp=timestamp,
            vebus_voltage_v=vebus_voltage_v,
        )
        return self.last_model

    @property
    def qualified(self) -> bool:
        return bool(
            self.last_model
            and self.last_model["lifecycle"]["state"] == "online"
            and self.last_model["paths"].get("/Connected") == 1
        )
