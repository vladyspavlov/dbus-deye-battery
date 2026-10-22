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
# Persistent operator consent, created by handoff-can-owner.  Polled rather than
# read once at startup: the handoff needs this service to report the feature
# enabled before it stops the stock driver, and a service that only learned of
# the marker at startup could never do so on a fresh install.
DEFAULT_ENABLE_FILE = Path("/data/deye-virtual-battery/can-tx-enabled")
STOCK_DRIVER_EXE = "/opt/victronenergy/can-bus-bms/can-bus-bms"


#: How often an absent stock driver is looked for again.  A send always
#: looks first, so this only bounds how stale the published status can be.
STOCK_RESCAN_SECONDS = 1.0


def find_stock_driver(executable: str = STOCK_DRIVER_EXE) -> tuple[bool, str | None]:
    """Return whether the exact stock binary is running, and its pid if found."""

    try:
        process_entries = os.scandir("/proc")
    except OSError:
        # Failure to prove that the stock driver is absent must inhibit TX.
        return True, None
    with process_entries:
        for entry in process_entries:
            if not entry.name.isdigit():
                continue
            if _process_executable(entry.name) == executable:
                return True, entry.name
    return False, None


def _process_executable(pid: str) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def stock_driver_running(executable: str = STOCK_DRIVER_EXE) -> bool:
    """Return true only for a live process executing the exact stock binary."""

    return find_stock_driver(executable)[0]


class StockDriverWatch:
    """Answer "is the stock driver running?" without walking /proc each poll.

    A full scan reads the executable link of every process: about 13 ms on a
    GX with 300 processes, so scanning on every 50 ms poll kept a fifth of a
    core busy.  While the stock driver runs, only its own pid is checked.
    While it is absent, /proc is scanned again at most every
    ``rescan_seconds``, and always when the caller asks for a ``fresh``
    answer, which the transmitter does before every send.
    """

    def __init__(
        self,
        executable: str = STOCK_DRIVER_EXE,
        *,
        rescan_seconds: float = STOCK_RESCAN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executable = executable
        self.rescan_seconds = float(rescan_seconds)
        self.clock = clock
        self._pid: str | None = None
        self._running = True
        self._next_scan: float | None = None
        self.scans = 0

    def __call__(self, *, fresh: bool = False) -> bool:
        if self._pid is not None:
            if _process_executable(self._pid) == self.executable:
                return True
            # It has just exited; another instance may still be running.
            self._pid = None
            fresh = True
        now = self.clock()
        if fresh or self._next_scan is None or now >= self._next_scan:
            self._running, self._pid = find_stock_driver(self.executable)
            self._next_scan = now + self.rescan_seconds
            self.scans += 1
        return self._running


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
        enable_file: Path | None = None,
        stock_detector: Callable[..., bool] | None = None,
        socket_factory: Callable[[str], Any] = _open_tx_socket,
        period_seconds: float = 1.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self.interface = interface
        # The command-line flag enables the feature for the life of the
        # process; the enable file enables it only while it exists.
        self.feature_flag = bool(feature_enabled)
        self.arm_file = arm_file
        self.enable_file = enable_file
        # Called on every poll, and with ``fresh=True`` right before a send.
        self.stock_detector = (
            stock_detector if stock_detector is not None else StockDriverWatch()
        )
        self.socket_factory = socket_factory
        self.period_seconds = float(period_seconds)
        self.poll_seconds = float(poll_seconds)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._feature_enabled = self.feature_flag
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
                feature_enabled=self._feature_enabled,
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
            while not self._stop.is_set():
                owner = self._update_ownership(fresh=False)
                if owner != previous_owner:
                    logging.warning(
                        "CAN keepalive ownership changed: owner=%s armed=%s stock_running=%s",
                        owner,
                        self._armed,
                        self._stock_running,
                    )
                    next_send = time.monotonic()
                    previous_owner = owner
                now = time.monotonic()
                # The poll may rely on a stock-driver answer up to a rescan
                # interval old; a send never does.
                if owner and now >= next_send and self._update_ownership(fresh=True):
                    # Opened on first ownership, so a service that never takes
                    # the keepalive never holds a TX socket.  A failed open is
                    # retried once per period, like a failed send.
                    if can_socket is None:
                        can_socket = self._open_socket()
                    if can_socket is not None:
                        self._send_pair(can_socket)
                    next_send = now + self.period_seconds
                self._stop.wait(self.poll_seconds)
        finally:
            with self._lock:
                self._owner = False
            if can_socket is not None:
                can_socket.close()

    def _update_ownership(self, *, fresh: bool) -> bool:
        feature_enabled = self._read_feature_enabled()
        armed = self.arm_file.is_file()
        stock_running = (
            self.stock_detector(fresh=True) if fresh else self.stock_detector()
        )
        owner = ownership_allowed(
            feature_enabled=feature_enabled,
            armed=armed,
            stock_running=stock_running,
        )
        with self._lock:
            self._feature_enabled = feature_enabled
            self._armed = armed
            self._stock_running = stock_running
            self._owner = owner
        return owner

    def _read_feature_enabled(self) -> bool:
        if self.feature_flag:
            return True
        return self.enable_file is not None and self.enable_file.is_file()

    def _open_socket(self) -> Any:
        """Open the TX socket on first ownership; count a failure, retry later."""

        try:
            return self.socket_factory(self.interface)
        except OSError as error:
            with self._lock:
                self._tx_errors += 1
                self._last_error = str(error)
            logging.error("Deye CAN keepalive socket could not be opened: %s", error)
            return None

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
