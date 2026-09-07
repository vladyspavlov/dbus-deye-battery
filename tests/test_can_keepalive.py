from pathlib import Path
import struct
import threading
import time

from deye_virtual_battery.can_keepalive import (
    KEEPALIVE_FRAMES,
    KeepaliveTransmitter,
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
        stock_detector=lambda: stock["running"],
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
