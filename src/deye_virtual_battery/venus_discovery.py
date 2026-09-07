"""Discover Venus D-Bus service names instead of hard-coding a port.

The VE.Bus service name embeds the port the inverter is attached to, so it
differs between GX models and between installations: ``ttyS3`` on one unit,
``ttyO2``, ``ttyUSB0`` or ``socketcan_can0`` on another.  Hard-coding it makes
an adapter work on exactly one system.

This module holds no D-Bus import of its own.  The caller passes an already
connected bus object, which keeps the whole module importable, and testable,
on a development machine that has no Venus D-Bus at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable


VEBUS_PREFIX = "com.victronenergy.vebus."

# Re-resolving on every failed read would hammer the bus while VE.Bus is
# restarting, which is exactly when reads fail.  Back off between attempts.
REDISCOVER_INTERVAL_SECONDS = 10.0


def discover_services(bus: Any, prefix: str) -> list[str]:
    """Return the sorted, currently owned bus names starting with ``prefix``."""
    try:
        names = [str(name) for name in bus.list_names()]
    except Exception:
        logging.exception("could not enumerate D-Bus names")
        return []
    return sorted(name for name in names if name.startswith(prefix))


def discover_vebus_service(bus: Any) -> str | None:
    """Return the VE.Bus service name, or ``None`` when none is present.

    A GX device normally hosts exactly one VE.Bus service.  When several are
    present the first in sorted order is chosen so the result is stable across
    restarts, and the ambiguity is logged rather than guessed at silently.
    """
    services = discover_services(bus, VEBUS_PREFIX)
    if not services:
        return None
    if len(services) > 1:
        logging.warning(
            "multiple VE.Bus services present (%s); using %s. "
            "Pass --vebus-service to select a different one.",
            ", ".join(services),
            services[0],
        )
    return services[0]


@dataclass
class VebusLocator:
    """Resolve the VE.Bus service once and re-resolve when it goes away.

    A VE.Bus restart changes nothing about the service *name*, but the name is
    unowned while the restart is in progress.  Reads then fail, and the
    adapter must neither crash nor spin: it reports ``None`` for the voltage,
    which the policy layer already treats as "no cross-check available", and
    retries at a bounded interval.
    """

    explicit: str | None = None
    clock: Callable[[], float] = field(default=lambda: 0.0)
    interval_seconds: float = REDISCOVER_INTERVAL_SECONDS
    _resolved: str | None = field(default=None, init=False)
    _last_attempt: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.explicit:
            self._resolved = self.explicit

    @property
    def service_name(self) -> str | None:
        return self._resolved

    def _may_retry(self) -> bool:
        if self._last_attempt is None:
            return True
        return self.clock() - self._last_attempt >= self.interval_seconds

    def resolve(self, bus: Any) -> str | None:
        """Return the service name, discovering it when it is not yet known."""
        if self.explicit:
            return self.explicit
        if self._resolved is not None:
            return self._resolved
        if not self._may_retry():
            return None
        self._last_attempt = self.clock()
        self._resolved = discover_vebus_service(bus)
        if self._resolved is not None:
            logging.info("using VE.Bus service %s", self._resolved)
        return self._resolved

    def invalidate(self) -> None:
        """Forget a discovered name after a failed read, so it is resolved again.

        An explicitly configured name is never forgotten: the operator asked
        for that service, and silently switching to another one would move the
        voltage cross-check to a different inverter.
        """
        if not self.explicit:
            self._resolved = None


def read_vebus_voltage(
    bus: Any,
    locator: VebusLocator,
    read_number: Callable[[Any, str, str], float | None],
) -> float | None:
    """Read VE.Bus DC voltage, tolerating an absent or restarting service."""
    service_name = locator.resolve(bus)
    if service_name is None:
        return None
    voltage = read_number(bus, service_name, "/Dc/0/Voltage")
    if voltage is None:
        locator.invalidate()
    return voltage
