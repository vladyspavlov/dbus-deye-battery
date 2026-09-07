"""Guarded ownership and transmission of the Deye inverter keepalive pair."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Callable

from .venus_runtime import encode_socketcan_frame


KEEPALIVE_FRAMES = (
    (0x305, bytes.fromhex("0000000000000000")),
    (0x307, bytes.fromhex("1234567856494300")),
)
DEFAULT_ARM_FILE = Path("/run/deye-virtual-battery/tx-armed")
STOCK_DRIVER_EXE = "/opt/victronenergy/can-bus-bms/can-bus-bms"


def stock_driver_running(executable: str = STOCK_DRIVER_EXE) -> bool:
    """Return true only for a live process executing the exact stock binary."""

    try:
        process_entries = os.scandir("/proc")
    except OSError:
        # Failure to prove that the stock driver is absent must inhibit TX.
        return True
    with process_entries:
        for entry in process_entries:
            if not entry.name.isdigit():
                continue
            try:
                if os.readlink(f"/proc/{entry.name}/exe") == executable:
                    return True
            except OSError:
                continue
    return False


def ownership_allowed(*, feature_enabled: bool, armed: bool, stock_running: bool) -> bool:
    return bool(feature_enabled and armed and not stock_running)


def _open_tx_socket(interface: str) -> socket.socket:
    can_socket = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    can_socket.bind((interface,))
    return can_socket


@dataclass(frozen=True, slots=True)
class KeepaliveSnapshot:
    feature_enabled: bool
    armed: bool
    stock_driver_running: bool
    owner: bool
    pairs_sent: int
    frames_sent: int
    last_tx_timestamp: float | None
    tx_errors: int
    last_error: str


class KeepaliveTransmitter:
    """Transmit only while explicitly armed and the stock executable is absent."""

    def __init__(
        self,
        *,
        interface: str,
        feature_enabled: bool,
        arm_file: Path = DEFAULT_ARM_FILE,
        stock_detector: Callable[[], bool] = stock_driver_running,
        socket_factory: Callable[[str], Any] = _open_tx_socket,
        period_seconds: float = 1.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self.interface = interface
        self.feature_enabled = bool(feature_enabled)
        self.arm_file = arm_file
        self.stock_detector = stock_detector
        self.socket_factory = socket_factory
        self.period_seconds = float(period_seconds)
        self.poll_seconds = float(poll_seconds)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._armed = False
        self._stock_running = True
        self._owner = False
        self._pairs_sent = 0
        self._frames_sent = 0
        self._last_tx_timestamp: float | None = None
        self._tx_errors = 0
        self._last_error = ""

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("keepalive transmitter already started")
        self._thread = threading.Thread(
            target=self._run,
            name="deye-can-keepalive",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> KeepaliveSnapshot:
        with self._lock:
            return KeepaliveSnapshot(
                feature_enabled=self.feature_enabled,
                armed=self._armed,
                stock_driver_running=self._stock_running,
                owner=self._owner,
                pairs_sent=self._pairs_sent,
                frames_sent=self._frames_sent,
                last_tx_timestamp=self._last_tx_timestamp,
                tx_errors=self._tx_errors,
                last_error=self._last_error,
            )

    def _run(self) -> None:
        can_socket = None
        next_send = time.monotonic()
        previous_owner = False
        try:
            if self.feature_enabled:
                can_socket = self.socket_factory(self.interface)
            while not self._stop.is_set():
                armed = self.arm_file.is_file()
                stock_running = self.stock_detector()
                owner = ownership_allowed(
                    feature_enabled=self.feature_enabled,
                    armed=armed,
                    stock_running=stock_running,
                )
                with self._lock:
                    self._armed = armed
                    self._stock_running = stock_running
                    self._owner = owner
                if owner != previous_owner:
                    logging.warning(
                        "CAN keepalive ownership changed: owner=%s armed=%s stock_running=%s",
                        owner,
                        armed,
                        stock_running,
                    )
                    next_send = time.monotonic()
                    previous_owner = owner
                now = time.monotonic()
                if owner and can_socket is not None and now >= next_send:
                    self._send_pair(can_socket)
                    next_send = now + self.period_seconds
                self._stop.wait(self.poll_seconds)
        finally:
            with self._lock:
                self._owner = False
            if can_socket is not None:
                can_socket.close()

    def _send_pair(self, can_socket: Any) -> None:
        try:
            for can_id, payload in KEEPALIVE_FRAMES:
                can_socket.send(encode_socketcan_frame(can_id, payload))
        except OSError as error:
            with self._lock:
                self._tx_errors += 1
                self._last_error = str(error)
            logging.error("Deye CAN keepalive transmission failed: %s", error)
            return
        sent_at = time.time()
        with self._lock:
            self._pairs_sent += 1
            self._frames_sent += len(KEEPALIVE_FRAMES)
            self._last_tx_timestamp = sent_at
            self._last_error = ""
