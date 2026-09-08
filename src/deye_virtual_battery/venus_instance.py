"""Resolve the battery device instance the way Venus OS expects it to be done.

A VRM device instance must be unique *per device class*: every
``com.victronenergy.battery`` on the bus needs its own number.  Victron's
D-Bus API documentation describes the mechanism drivers are supposed to use --
ask localsettings to reserve one, and take whatever it grants:

    localsettings.AddSetting('/Settings/Devices/<unique-id>/ClassAndVrmInstance',
                             'battery:513')

localsettings stores the mapping against ``<unique-id>``, grants the preferred
number when it is free, and otherwise hands out the next free one.  The stock
``can-bus-bms`` driver does exactly this; the string
``/Settings/Devices/%s/ClassAndVrmInstance`` is in its binary.

Why this matters here.  The stock CAN BMS driver on ``can0`` takes battery
instance **512**, which is why this adapter's default is 513.  That offset is
only safe for that one arrangement.  On a GX where BMS-Can is ``can1`` -- which
is the normal wiring on a Cerbo GX, and what this project's own documentation
recommends -- the stock driver plausibly lands on 513 itself, and two packs
running this adapter collide with each other outright.

The counter-risk is worse than the collision, so it is guarded here rather
than left to the operator.  ``/DeviceInstance`` is what
``/Settings/SystemSetup/BatteryService`` points at, and what VRM keys a
device's history on.  Silently moving it deselects the battery monitor and
splits the VRM history in two.  So this module never moves an instance that
the system is currently selecting, and reuses an existing reservation in
preference to asking for a new one.

Nothing here imports D-Bus.  The decision is a pure function over two
injected callables, so every branch is tested on a development PC.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Sequence


SETTINGS_SERVICE = "com.victronenergy.settings"
BATTERY_SERVICE_SETTING = "/Settings/SystemSetup/BatteryService"
RESERVATION_TEMPLATE = "/Settings/Devices/{settings_id}/ClassAndVrmInstance"
DEVICE_CLASS = "battery"
# Victron's D-Bus API document: "Max value of /DeviceInstance is 32767. This is
# because its defined like that in the VRM db."
MAX_DEVICE_INSTANCE = 32767
# The stock can-bus-bms driver's instance on can0, confirmed in a VRM export
# that carried both it and this adapter.  Documented so the 513 default is not
# a magic number to the next reader.
STOCK_CAN_BMS_INSTANCE = 512

# localsettings turns the path into XML element names, so a settings id has to
# be a plain identifier-ish token.  Keep it short: it appears in settings.xml
# and in the D-Bus path.
_UNSAFE = re.compile(r"[^A-Za-z0-9]+")
MAX_SETTINGS_ID_LENGTH = 40

CONFIGURED = "configured"
PINNED_TO_SELECTION = "pinned-to-selection"
RESERVED = "reserved"
ALLOCATED = "allocated"
FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class InstanceResolution:
    """What instance to publish, where it came from, and what it cost."""

    instance: int
    source: str
    settings_id: str
    requested: int
    attempted_settings_write: bool
    detail: str

    @property
    def moved(self) -> bool:
        return self.instance != self.requested


def sanitize_settings_id(text: str | None) -> str:
    """Reduce a serial or interface name to a localsettings-safe token."""

    token = _UNSAFE.sub("_", (text or "").strip()).strip("_")
    return token[:MAX_SETTINGS_ID_LENGTH]


def settings_id_candidates(
    *,
    explicit: str | None = None,
    serial: str | None = None,
    interface: str | None = None,
) -> list[str]:
    """Identities to look a reservation up under, most stable first.

    The pack serial is the only value that survives moving the cable to a
    different CAN port, so it leads.  It arrives from ``0x600``/``0x650`` and
    may be absent, hence the interface fallback.
    """

    candidates: list[str] = []
    for prefix, value in (("", explicit), ("deye_", serial), ("deye_", interface)):
        token = sanitize_settings_id(value)
        if not token:
            continue
        candidate = f"{prefix}{token}"[:MAX_SETTINGS_ID_LENGTH]
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def format_class_and_instance(instance: int, device_class: str = DEVICE_CLASS) -> str:
    return f"{device_class}:{int(instance)}"


def parse_class_and_instance(value: Any) -> tuple[str, int] | None:
    """Read back a ``ClassAndVrmInstance`` value, tolerating D-Bus wrappers."""

    if value is None or isinstance(value, (list, tuple, dict)):
        return None
    text = str(value).strip()
    device_class, separator, number = text.rpartition(":")
    if not separator or not device_class:
        return None
    try:
        instance = int(number)
    except ValueError:
        return None
    if not 0 <= instance <= MAX_DEVICE_INSTANCE:
        return None
    return device_class, instance


def selected_battery_instance(value: Any) -> int | None:
    """Extract the instance from ``/Settings/SystemSetup/BatteryService``.

    The setting reads ``com.victronenergy.battery/513`` when a battery is
    explicitly selected, and ``default``, ``nobattery`` or an empty value when
    it is not.
    """

    if value is None or isinstance(value, (list, tuple, dict)):
        return None
    service, separator, number = str(value).strip().rpartition("/")
    if not separator or not service.startswith("com.victronenergy.battery"):
        return None
    try:
        instance = int(number)
    except ValueError:
        return None
    return instance if 0 <= instance <= MAX_DEVICE_INSTANCE else None


def resolve_device_instance(
    *,
    preferred: int,
    candidates: Sequence[str] = (),
    read_reservation: Callable[[str], Any] | None = None,
    allocate: Callable[[str, int], Any] | None = None,
    selected_instance: int | None = None,
) -> InstanceResolution:
    """Decide which device instance to publish.

    ``allocate`` being ``None`` means auto-allocation is switched off, and the
    configured number is published verbatim -- the historical behaviour, and
    the only mode that provably writes nothing to localsettings.
    """

    if not 0 <= int(preferred) <= MAX_DEVICE_INSTANCE:
        raise ValueError(f"device instance out of range: {preferred}")
    preferred = int(preferred)

    def settled(instance: int, source: str, settings_id: str, wrote: bool, detail: str):
        return InstanceResolution(
            instance=instance,
            source=source,
            settings_id=settings_id,
            requested=preferred,
            attempted_settings_write=wrote,
            detail=detail,
        )

    if allocate is None:
        return settled(preferred, CONFIGURED, "", False, "auto-allocation disabled")

    # Never move an instance the system is currently pointing at.  Registering
    # a reservation here could persist a different number and deselect the
    # battery monitor on the next start, so do not even ask.
    if selected_instance is not None and selected_instance == preferred:
        return settled(
            preferred,
            PINNED_TO_SELECTION,
            "",
            False,
            f"{BATTERY_SERVICE_SETTING} already selects instance {preferred}",
        )

    if read_reservation is not None:
        for settings_id in candidates:
            try:
                raw = read_reservation(settings_id)
            except Exception as error:  # localsettings absent or path missing
                return settled(
                    preferred, FALLBACK, "", False, f"reservation lookup failed: {error}"
                )
            parsed = parse_class_and_instance(raw)
            if parsed is None:
                continue
            device_class, instance = parsed
            if device_class != DEVICE_CLASS:
                continue
            return settled(
                instance,
                RESERVED,
                settings_id,
                False,
                f"existing reservation {settings_id} -> {raw}",
            )

    if not candidates:
        return settled(
            preferred, CONFIGURED, "", False, "no stable identity to reserve under"
        )

    settings_id = candidates[0]
    try:
        granted = allocate(settings_id, preferred)
    except Exception as error:
        return settled(
            preferred, FALLBACK, settings_id, True, f"reservation failed: {error}"
        )
    parsed = parse_class_and_instance(granted)
    if parsed is None or parsed[0] != DEVICE_CLASS:
        return settled(
            preferred,
            FALLBACK,
            settings_id,
            True,
            f"localsettings returned an unusable value: {granted!r}",
        )
    return settled(
        parsed[1],
        ALLOCATED,
        settings_id,
        True,
        f"localsettings granted {granted} for {settings_id}",
    )


def make_reservation_reader(
    read_value: Callable[[str, str], Any],
) -> Callable[[str], Any]:
    """Read one reservation, treating an absent setting as absent, not an error."""

    def read_reservation(settings_id: str) -> Any:
        try:
            return read_value(
                SETTINGS_SERVICE, RESERVATION_TEMPLATE.format(settings_id=settings_id)
            )
        except Exception:
            return None

    return read_reservation


def make_settings_allocator(
    bus: Any, settings_device_factory: Callable[..., Any]
) -> Callable[[str, int], Any]:
    """Reserve an instance through velib_python's SettingsDevice.

    This is the one call in the project that writes to localsettings, and it
    writes a single identity mapping -- never a charge, discharge or VE.Bus
    setting.
    """

    def allocate(settings_id: str, preferred: int) -> Any:
        path = RESERVATION_TEMPLATE.format(settings_id=settings_id)
        device = settings_device_factory(
            bus,
            {"instance": [path, format_class_and_instance(preferred), 0, 0]},
            None,
        )
        return device["instance"]

    return allocate
