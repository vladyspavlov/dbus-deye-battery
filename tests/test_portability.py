"""The adapter must run on a GX it was not developed on.

Two things used to be baked in: the VE.Bus service name, which encodes the
port the inverter happens to be wired to, and the CAN interface, which is
``can0`` on some GX models and ``can1`` on others.  These tests hold the
discovery and validation behaviour that replaced them.
"""

import pytest

from deye_virtual_battery.policy import PolicyConfig
from deye_virtual_battery.venus_bms_publisher import (
    _fixed_paths,
    _validate,
    build_parser,
)
from deye_virtual_battery.venus_discovery import (
    VebusLocator,
    discover_vebus_service,
    read_vebus_voltage,
)


class FakeBus:
    """Stands in for a Venus system bus without importing dbus."""

    def __init__(self, names, *, raises=False):
        self.names = list(names)
        self.raises = raises
        self.calls = 0

    def list_names(self):
        self.calls += 1
        if self.raises:
            raise OSError("bus unavailable")
        return list(self.names)


def args(*argv):
    return build_parser().parse_args(list(argv))


# --- VE.Bus discovery ----------------------------------------------------


def test_vebus_service_is_discovered_whatever_port_it_is_on():
    for port in ("ttyS3", "ttyO2", "ttyUSB0", "socketcan_can0"):
        bus = FakeBus(["org.freedesktop.DBus", f"com.victronenergy.vebus.{port}"])
        assert discover_vebus_service(bus) == f"com.victronenergy.vebus.{port}"


def test_absent_vebus_service_is_reported_rather_than_guessed():
    assert discover_vebus_service(FakeBus(["com.victronenergy.system"])) is None


def test_multiple_vebus_services_resolve_deterministically():
    names = ["com.victronenergy.vebus.ttyUSB0", "com.victronenergy.vebus.ttyS3"]
    # Sorted, so a restart cannot silently move the cross-check to the other
    # inverter.
    assert discover_vebus_service(FakeBus(names)) == "com.victronenergy.vebus.ttyS3"
    assert discover_vebus_service(FakeBus(list(reversed(names)))) == (
        "com.victronenergy.vebus.ttyS3"
    )


def test_bus_enumeration_failure_is_not_fatal():
    assert discover_vebus_service(FakeBus([], raises=True)) is None


def test_discovery_happens_once_while_the_service_stays_present():
    bus = FakeBus(["com.victronenergy.vebus.ttyS3"])
    locator = VebusLocator(clock=lambda: 0.0)
    for _ in range(5):
        assert locator.resolve(bus) == "com.victronenergy.vebus.ttyS3"
    assert bus.calls == 1


def test_an_explicit_service_name_is_never_rediscovered():
    bus = FakeBus(["com.victronenergy.vebus.ttyUSB0"])
    locator = VebusLocator(explicit="com.victronenergy.vebus.ttyS3")
    assert locator.resolve(bus) == "com.victronenergy.vebus.ttyS3"
    locator.invalidate()
    assert locator.resolve(bus) == "com.victronenergy.vebus.ttyS3"
    assert bus.calls == 0


def test_rediscovery_is_rate_limited_while_vebus_is_restarting():
    """A failed read must not turn into a discovery loop on every update tick."""
    now = [0.0]
    bus = FakeBus([])
    locator = VebusLocator(clock=lambda: now[0], interval_seconds=10.0)

    assert locator.resolve(bus) is None
    assert bus.calls == 1
    now[0] = 5.0
    assert locator.resolve(bus) is None
    assert bus.calls == 1, "retried before the backoff interval elapsed"

    now[0] = 10.0
    bus.names = ["com.victronenergy.vebus.ttyS3"]
    assert locator.resolve(bus) == "com.victronenergy.vebus.ttyS3"
    assert bus.calls == 2


def test_a_failed_read_forgets_the_name_so_it_is_resolved_again():
    now = [0.0]
    bus = FakeBus(["com.victronenergy.vebus.ttyS3"])
    locator = VebusLocator(clock=lambda: now[0], interval_seconds=1.0)

    assert read_vebus_voltage(bus, locator, lambda *_: 53.2) == 53.2
    assert locator.service_name == "com.victronenergy.vebus.ttyS3"

    # VE.Bus restarts: the read fails and the cached name is dropped.
    assert read_vebus_voltage(bus, locator, lambda *_: None) is None
    assert locator.service_name is None

    now[0] = 2.0
    assert read_vebus_voltage(bus, locator, lambda *_: 53.4) == 53.4


def test_missing_vebus_yields_no_voltage_rather_than_an_exception():
    locator = VebusLocator(clock=lambda: 0.0)

    def must_not_be_called(*_):
        raise AssertionError("read attempted without a resolved service")

    assert read_vebus_voltage(FakeBus([]), locator, must_not_be_called) is None


# --- configuration validation -------------------------------------------


def test_stock_defaults_are_accepted():
    _validate(args())


@pytest.mark.parametrize("interface", ["can0", "can1", "vecan0", "can8"])
def test_any_plausible_can_interface_is_accepted(interface):
    _validate(args("--interface", interface))


@pytest.mark.parametrize(
    "argv",
    [
        ("--interface", "can 0"),
        ("--interface", ""),
        ("--interface", "0can"),
        ("--interface", "c" * 16),
        ("--service-name", "com.victronenergy.solarcharger.x"),
        ("--service-name", "com.victronenergy.battery."),
        ("--service-name", "com.victronenergy.battery.has space"),
        ("--device-instance", "-1"),
        ("--device-instance", "10000"),
        ("--vebus-service", "com.victronenergy.system"),
        ("--qualification-seconds", "1"),
        ("--qualification-seconds", "600"),
    ],
)
def test_unsafe_configuration_is_refused(argv):
    with pytest.raises(ValueError):
        _validate(args(*argv))


def test_the_published_connection_names_the_interface_actually_used():
    paths = _fixed_paths(PolicyConfig(), interface="can1")
    assert "can1" in paths["/Mgmt/Connection"]


def test_a_second_pack_can_be_published_without_colliding():
    """Two batteries on one GX need distinct names and instances."""
    _validate(
        args(
            "--interface", "can1",
            "--service-name", "com.victronenergy.battery.deye_se_f12_b",
            "--device-instance", "514",
        )
    )
