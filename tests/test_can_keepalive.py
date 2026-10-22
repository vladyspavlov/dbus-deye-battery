import os
from pathlib import Path
import struct
import threading
import time

import pytest

from deye_virtual_battery.can_keepalive import (
    KEEPALIVE_FRAMES,
    KeepaliveTransmitter,
    StockDriverWatch,
    ownership_allowed,
)
from deye_virtual_battery.venus_runtime import CAN_FRAME_FORMAT


class FakeSocket:
    def __init__(self):
        self.frames: list[bytes] = []
        self.lock = threading.Lock()
        self.closed = False

    def send(self, raw: bytes) -> int:
        with self.lock:
            self.frames.append(raw)
        return len(raw)

    def close(self) -> None:
        self.closed = True


def wait_for(predicate, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached before deadline")


def test_ownership_requires_feature_arm_and_proven_stock_absence():
    assert ownership_allowed(feature_enabled=True, armed=True, stock_running=False)
    assert not ownership_allowed(feature_enabled=False, armed=True, stock_running=False)
    assert not ownership_allowed(feature_enabled=True, armed=False, stock_running=False)
    assert not ownership_allowed(feature_enabled=True, armed=True, stock_running=True)


def test_transmitter_waits_for_stock_exit_and_stops_when_disarmed(tmp_path: Path):
    arm_file = tmp_path / "tx-armed"
    arm_file.touch()
    stock = {"running": True}
    fake_socket = FakeSocket()
    transmitter = KeepaliveTransmitter(
        interface="can0",
        feature_enabled=True,
        arm_file=arm_file,
        stock_detector=lambda fresh=False: stock["running"],
        socket_factory=lambda _: fake_socket,
        period_seconds=0.04,
        poll_seconds=0.002,
    )
    transmitter.start()
    try:
        wait_for(lambda: transmitter.snapshot().armed)
        time.sleep(0.03)
        assert fake_socket.frames == []
        assert transmitter.snapshot().stock_driver_running is True

        stock["running"] = False
        wait_for(lambda: len(fake_socket.frames) >= 2)
        snapshot = transmitter.snapshot()
        assert snapshot.owner is True
        assert snapshot.pairs_sent >= 1
        assert snapshot.tx_errors == 0

        first_pair = fake_socket.frames[:2]
        decoded = []
        for raw in first_pair:
            can_id, dlc, payload = struct.unpack(CAN_FRAME_FORMAT, raw)
            decoded.append((can_id, payload[:dlc]))
        assert tuple(decoded) == KEEPALIVE_FRAMES

        arm_file.unlink()
        wait_for(lambda: not transmitter.snapshot().owner)
        sent_after_disarm = len(fake_socket.frames)
        time.sleep(0.06)
        assert len(fake_socket.frames) == sent_after_disarm
    finally:
        transmitter.stop()
    assert fake_socket.closed is True


def test_enable_file_is_polled_and_gates_transmission(tmp_path: Path):
    """Issue #2: the feature follows the marker without a service restart."""
    arm_file = tmp_path / "tx-armed"
    arm_file.touch()
    enable_file = tmp_path / "can-tx-enabled"
    opened: list[FakeSocket] = []

    def factory(_: str) -> FakeSocket:
        opened.append(FakeSocket())
        return opened[-1]

    transmitter = KeepaliveTransmitter(
        interface="can0",
        feature_enabled=False,
        arm_file=arm_file,
        enable_file=enable_file,
        stock_detector=lambda fresh=False: False,
        socket_factory=factory,
        period_seconds=0.04,
        poll_seconds=0.002,
    )
    transmitter.start()
    try:
        wait_for(lambda: transmitter.snapshot().armed)
        time.sleep(0.03)
        snapshot = transmitter.snapshot()
        assert snapshot.feature_enabled is False
        assert snapshot.owner is False
        assert opened == []

        enable_file.touch()
        wait_for(lambda: opened and len(opened[0].frames) >= 2)
        assert transmitter.snapshot().feature_enabled is True
        assert transmitter.snapshot().owner is True

        enable_file.unlink()
        wait_for(lambda: not transmitter.snapshot().owner)
        assert transmitter.snapshot().feature_enabled is False
        sent = len(opened[0].frames)
        time.sleep(0.06)
        assert len(opened[0].frames) == sent
    finally:
        transmitter.stop()
    assert len(opened) == 1


def test_socket_open_failure_is_counted_not_fatal(tmp_path: Path):
    arm_file = tmp_path / "tx-armed"
    arm_file.touch()
    attempts = {"count": 0}
    fake_socket = FakeSocket()

    def factory(_: str) -> FakeSocket:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError("No such device")
        return fake_socket

    transmitter = KeepaliveTransmitter(
        interface="can0",
        feature_enabled=True,
        arm_file=arm_file,
        stock_detector=lambda fresh=False: False,
        socket_factory=factory,
        period_seconds=0.04,
        poll_seconds=0.002,
    )
    transmitter.start()
    try:
        wait_for(lambda: len(fake_socket.frames) >= 2)
        snapshot = transmitter.snapshot()
        assert snapshot.tx_errors == 1
        assert snapshot.last_error == ""
    finally:
        transmitter.stop()


def test_every_send_follows_a_fresh_stock_driver_check(tmp_path: Path):
    """A cached "stock driver absent" never authorises a send on its own."""
    arm_file = tmp_path / "tx-armed"
    arm_file.touch()
    calls = {"fresh": 0}
    fresh_says_running = {"value": False}

    def detector(fresh: bool = False) -> bool:
        if fresh:
            calls["fresh"] += 1
            return fresh_says_running["value"]
        return False

    fake_socket = FakeSocket()
    transmitter = KeepaliveTransmitter(
        interface="can0",
        feature_enabled=True,
        arm_file=arm_file,
        stock_detector=detector,
        socket_factory=lambda _: fake_socket,
        period_seconds=0.02,
        poll_seconds=0.002,
    )
    transmitter.start()
    try:
        wait_for(lambda: len(fake_socket.frames) >= 6)
        fresh_says_running["value"] = True
        wait_for(lambda: not transmitter.snapshot().owner)
        sent = len(fake_socket.frames)
        time.sleep(0.06)
        assert len(fake_socket.frames) == sent
        assert calls["fresh"] >= sent // 2
    finally:
        transmitter.stop()


needs_proc = pytest.mark.skipif(
    not os.path.exists("/proc/self/exe"), reason="needs Linux /proc"
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@needs_proc
def test_watch_rescans_an_absent_stock_driver_at_most_once_per_interval():
    clock = Clock()
    watch = StockDriverWatch("/nonexistent/can-bus-bms", rescan_seconds=1.0, clock=clock)
    assert watch() is False
    for step in range(1, 20):
        clock.now = step * 0.05
        assert watch() is False
    assert watch.scans == 1
    clock.now = 1.0
    watch()
    assert watch.scans == 2
    assert watch(fresh=True) is False
    assert watch.scans == 3


@needs_proc
def test_watch_follows_a_running_stock_driver_by_its_pid():
    # This test's own interpreter stands in for the stock executable.
    clock = Clock()
    watch = StockDriverWatch(os.readlink("/proc/self/exe"), clock=clock)
    assert watch() is True
    for _ in range(20):
        clock.now += 0.5
        assert watch(fresh=True) is True
    assert watch.scans == 1
