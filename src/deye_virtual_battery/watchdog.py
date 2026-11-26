"""Detect a publisher whose update loop has stopped running.

The update loop unregisters the battery service when it raises.  A loop that
simply stops (a D-Bus call that never returns, a deadlock) raised nothing, so
the last limits stayed on D-Bus and the keepalive thread, which runs on its
own, kept telling the battery that a PCS was present (issue #15).

The watchdog is fed by every update.  When it goes hungry the CAN keepalive
stops at once, and the owner is told so it can leave: a process that exits
drops its D-Bus name, systemcalc then sees the BMS disappear instead of a
frozen one, and the supervisor starts a fresh process.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable


#: Longer than any normal update, including a few slow D-Bus reads, and far
#: below the 300 s after which VE.Bus drops its operational limits.
DEFAULT_STALL_SECONDS = 30.0


class StallWatchdog:
    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_STALL_SECONDS,
        on_stall: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = 1.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("watchdog timeout must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.on_stall = on_stall
        self.clock = clock
        self.poll_seconds = float(poll_seconds)
        self._lock = threading.Lock()
        self._last_beat = clock()
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        with self._lock:
            self._last_beat = self.clock()

    def age(self) -> float:
        with self._lock:
            return max(0.0, self.clock() - self._last_beat)

    def alive(self) -> bool:
        """False from the moment the loop has been silent for too long."""
        with self._lock:
            if self._fired:
                return False
        return self.age() <= self.timeout_seconds

    def check(self) -> bool:
        """Fire ``on_stall`` once when hungry.  Returns whether it is alive."""
        age = self.age()
        if age <= self.timeout_seconds:
            return True
        with self._lock:
            first = not self._fired
            self._fired = True
        if first:
            logging.critical(
                "publisher update loop silent for %.1f s (limit %.1f s)",
                age,
                self.timeout_seconds,
            )
            if self.on_stall is not None:
                self.on_stall(age)
        return False

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("watchdog already started")
        self.beat()
        self._thread = threading.Thread(
            target=self._run, name="deye-publisher-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.check()
