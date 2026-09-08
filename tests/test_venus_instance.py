"""Device-instance resolution must never deselect a working battery monitor.

/DeviceInstance is what /Settings/SystemSetup/BatteryService points at and what
VRM keys a device's history on.  Auto-allocation is the mechanism Victron
documents, but it is only safe because of the guards asserted here: a selected
instance is never moved, an existing reservation is reused rather than replaced,
and every failure falls back to the configured number instead of guessing.
"""

import pytest

from deye_virtual_battery.venus_instance import (
    ALLOCATED,
    CONFIGURED,
    DEVICE_CLASS,
    FALLBACK,
    MAX_DEVICE_INSTANCE,
    PINNED_TO_SELECTION,
    RESERVATION_TEMPLATE,
    RESERVED,
    STOCK_CAN_BMS_INSTANCE,
    format_class_and_instance,
    make_reservation_reader,
    make_settings_allocator,
    parse_class_and_instance,
    resolve_device_instance,
    sanitize_settings_id,
    selected_battery_instance,
    settings_id_candidates,
)


def allocator(granted, *, record=None):
    def allocate(settings_id, preferred):
        if record is not None:
            record.append((settings_id, preferred))
        return granted(settings_id, preferred) if callable(granted) else granted

    return allocate


def test_the_default_sits_one_above_the_stock_can_bms_driver():
    """513 is not a magic number: 512 is the stock can-bus-bms instance."""
    assert STOCK_CAN_BMS_INSTANCE == 512


def test_disabled_allocation_publishes_the_configured_number_and_writes_nothing():
    resolution = resolve_device_instance(preferred=513, allocate=None)
    assert resolution.instance == 513
    assert resolution.source == CONFIGURED
    assert resolution.attempted_settings_write is False
    assert resolution.moved is False


def test_a_selected_instance_is_never_moved():
    """The dangerous case: the system already points at what we publish."""
    record = []
    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_abc"],
        read_reservation=lambda _: "battery:777",
        allocate=allocator("battery:777", record=record),
        selected_instance=513,
    )
    assert resolution.instance == 513
    assert resolution.source == PINNED_TO_SELECTION
    assert resolution.attempted_settings_write is False
    # Not merely ignored -- localsettings is never asked, so no contradicting
    # reservation is persisted for the next start to honour.
    assert record == []


def test_an_existing_reservation_is_reused_without_writing():
    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_missing", "deye_known"],
        read_reservation=lambda i: "battery:514" if i == "deye_known" else None,
        allocate=allocator("battery:999"),
    )
    assert resolution.instance == 514
    assert resolution.source == RESERVED
    assert resolution.settings_id == "deye_known"
    assert resolution.attempted_settings_write is False


def test_a_reservation_of_another_device_class_is_ignored():
    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_abc"],
        read_reservation=lambda _: "temperature:12",
        allocate=allocator("battery:513"),
    )
    assert resolution.source == ALLOCATED
    assert resolution.instance == 513


def test_localsettings_grants_the_next_free_number_when_the_preferred_is_taken():
    """Two packs, no configuration: 513 and 514."""
    record = []
    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_SECONDPACK"],
        read_reservation=lambda _: None,
        allocate=allocator("battery:514", record=record),
    )
    assert resolution.instance == 514
    assert resolution.source == ALLOCATED
    assert resolution.settings_id == "deye_SECONDPACK"
    assert resolution.attempted_settings_write is True
    assert resolution.moved is True
    assert record == [("deye_SECONDPACK", 513)]


@pytest.mark.parametrize("granted", [None, "", "battery", "battery:", "battery:x",
                                     "temperature:5", ["battery:5"], "battery:99999"])
def test_an_unusable_grant_falls_back_to_the_configured_number(granted):
    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_abc"],
        read_reservation=lambda _: None,
        allocate=allocator(granted),
    )
    assert resolution.instance == 513
    assert resolution.source == FALLBACK


def test_a_failing_allocation_falls_back_and_still_reports_the_attempt():
    def explode(settings_id, preferred):
        raise RuntimeError("localsettings is not running")

    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_abc"],
        read_reservation=lambda _: None,
        allocate=explode,
    )
    assert resolution.instance == 513
    assert resolution.source == FALLBACK
    # Conservative on purpose: the write may have landed before the failure,
    # so /Diagnostics/Commissioning/NoSettingsWrites must not claim otherwise.
    assert resolution.attempted_settings_write is True


def test_a_failing_reservation_lookup_does_not_then_allocate():
    """If we cannot see the reservations, we must not create a new one."""
    def explode(_):
        raise RuntimeError("no settings service")

    record = []
    resolution = resolve_device_instance(
        preferred=513,
        candidates=["deye_abc"],
        read_reservation=explode,
        allocate=allocator("battery:600", record=record),
    )
    assert resolution.instance == 513
    assert resolution.source == FALLBACK
    assert record == []


def test_without_a_stable_identity_nothing_is_reserved():
    record = []
    resolution = resolve_device_instance(
        preferred=513,
        candidates=[],
        read_reservation=lambda _: None,
        allocate=allocator("battery:600", record=record),
    )
    assert resolution.source == CONFIGURED
    assert resolution.instance == 513
    assert record == []


def test_an_out_of_range_preferred_instance_is_refused():
    with pytest.raises(ValueError):
        resolve_device_instance(preferred=MAX_DEVICE_INSTANCE + 1)
    with pytest.raises(ValueError):
        resolve_device_instance(preferred=-1)


def test_the_serial_leads_the_candidate_order():
    """Only the serial survives moving the cable to another CAN port."""
    assert settings_id_candidates(serial="DY2401ABC", interface="can1") == [
        "deye_DY2401ABC",
        "deye_can1",
    ]
    assert settings_id_candidates(interface="can1") == ["deye_can1"]
    assert settings_id_candidates(
        explicit="my_pack", serial="DY1", interface="can0"
    ) == ["my_pack", "deye_DY1", "deye_can0"]
    assert settings_id_candidates() == []


def test_a_settings_id_is_reduced_to_a_path_safe_token():
    assert sanitize_settings_id(" DY-2401/ABC ") == "DY_2401_ABC"
    assert sanitize_settings_id("///") == ""
    assert sanitize_settings_id(None) == ""
    assert len(sanitize_settings_id("x" * 200)) == 40
    # A serial and an interface must never collapse onto one identity.
    assert settings_id_candidates(serial="can0", interface="can0") == ["deye_can0"]


def test_the_reservation_path_is_the_one_venus_documents():
    assert (
        RESERVATION_TEMPLATE.format(settings_id="deye_x")
        == "/Settings/Devices/deye_x/ClassAndVrmInstance"
    )
    assert format_class_and_instance(513) == "battery:513"
    assert parse_class_and_instance("battery:513") == (DEVICE_CLASS, 513)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("com.victronenergy.battery/513", 513),
        ("com.victronenergy.battery.deye_lv/514", 514),
        ("default", None),
        ("nobattery", None),
        ("", None),
        (None, None),
        ([], None),
        ("com.victronenergy.vebus/275", None),
        ("com.victronenergy.battery/notanumber", None),
    ],
)
def test_the_selected_battery_service_setting_is_parsed(value, expected):
    assert selected_battery_instance(value) == expected


def test_the_reservation_reader_treats_a_missing_setting_as_absent():
    def read_value(service, path):
        raise RuntimeError("org.freedesktop.DBus.Error.UnknownObject")

    assert make_reservation_reader(read_value)("deye_x") is None

    seen = []

    def ok(service, path):
        seen.append((service, path))
        return "battery:513"

    assert make_reservation_reader(ok)("deye_x") == "battery:513"
    assert seen == [
        ("com.victronenergy.settings", "/Settings/Devices/deye_x/ClassAndVrmInstance")
    ]


def test_the_allocator_asks_velib_for_exactly_one_identity_setting():
    """It must reserve an instance and touch nothing else."""
    calls = []

    class FakeSettingsDevice:
        def __init__(self, bus, supported, callback):
            calls.append((bus, supported, callback))
            self._supported = supported

        def __getitem__(self, key):
            return "battery:514"

    allocate = make_settings_allocator("bus-object", FakeSettingsDevice)
    assert allocate("deye_x", 513) == "battery:514"
    (bus, supported, callback) = calls[0]
    assert bus == "bus-object"
    assert callback is None
    assert list(supported) == ["instance"]
    path, default, minimum, maximum = supported["instance"]
    assert path == "/Settings/Devices/deye_x/ClassAndVrmInstance"
    assert default == "battery:513"
    assert (minimum, maximum) == (0, 0)
    # Nothing under /Settings/SystemSetup, DVCC or VE.Bus may appear here.
    assert path.startswith("/Settings/Devices/")


# --- how the publisher wires it up ---------------------------------------


class FakeBus:
    """A Venus system bus, without importing dbus."""

    def __init__(self, values):
        self.values = dict(values)
        self.reads = []

    def get_object(self, service, path):
        self.reads.append((service, path))
        values = self.values
        key = (service, path)
        if key not in values:
            raise RuntimeError("org.freedesktop.DBus.Error.UnknownObject")

        class Item:
            def GetValue(self, dbus_interface=None):
                return values[key]

        return Item()


def publisher_args(**overrides):
    from deye_virtual_battery.venus_bms_publisher import build_parser

    argv = []
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not None and value is not False:
            argv.extend([flag, str(value)])
    return build_parser().parse_args(argv)


class FakeSettingsDevice:
    granted = "battery:514"
    calls: list = []

    def __init__(self, bus, supported, callback):
        FakeSettingsDevice.calls.append(supported)

    def __getitem__(self, key):
        return FakeSettingsDevice.granted


def test_the_publisher_publishes_the_configured_instance_by_default():
    from deye_virtual_battery.venus_bms_publisher import _resolve_instance

    FakeSettingsDevice.calls = []
    bus = FakeBus({})
    resolution = _resolve_instance(
        bus, publisher_args(), serial=None, settings_device_factory=FakeSettingsDevice
    )
    assert resolution.instance == 513
    assert resolution.source == CONFIGURED
    assert FakeSettingsDevice.calls == []


def test_the_publisher_reserves_under_the_pack_serial_when_asked():
    from deye_virtual_battery.venus_bms_publisher import _resolve_instance

    FakeSettingsDevice.calls = []
    bus = FakeBus({})
    resolution = _resolve_instance(
        bus,
        publisher_args(auto_device_instance=True, interface="can1"),
        serial="DY2401-0007",
        settings_device_factory=FakeSettingsDevice,
    )
    assert resolution.instance == 514
    assert resolution.source == ALLOCATED
    assert resolution.settings_id == "deye_DY2401_0007"
    assert FakeSettingsDevice.calls[0]["instance"][0] == (
        "/Settings/Devices/deye_DY2401_0007/ClassAndVrmInstance"
    )


def test_the_publisher_will_not_move_the_selected_battery_monitor():
    """The live failure mode: an upgrade silently deselects the battery."""
    from deye_virtual_battery.venus_bms_publisher import _resolve_instance
    from deye_virtual_battery.venus_instance import BATTERY_SERVICE_SETTING

    FakeSettingsDevice.calls = []
    bus = FakeBus(
        {
            ("com.victronenergy.settings", BATTERY_SERVICE_SETTING): (
                "com.victronenergy.battery/513"
            )
        }
    )
    resolution = _resolve_instance(
        bus,
        publisher_args(auto_device_instance=True),
        serial="DY2401-0007",
        settings_device_factory=FakeSettingsDevice,
    )
    assert resolution.instance == 513
    assert resolution.source == PINNED_TO_SELECTION
    assert FakeSettingsDevice.calls == []


def test_the_publisher_reuses_a_reservation_rather_than_making_another():
    from deye_virtual_battery.venus_bms_publisher import _resolve_instance

    FakeSettingsDevice.calls = []
    bus = FakeBus(
        {
            (
                "com.victronenergy.settings",
                "/Settings/Devices/deye_can0/ClassAndVrmInstance",
            ): "battery:517"
        }
    )
    resolution = _resolve_instance(
        bus,
        publisher_args(auto_device_instance=True),
        serial=None,
        settings_device_factory=FakeSettingsDevice,
    )
    assert resolution.instance == 517
    assert resolution.source == RESERVED
    assert FakeSettingsDevice.calls == []


def test_two_packs_come_up_as_513_and_514_with_no_configuration():
    """The claim the README makes, against a localsettings that behaves.

    localsettings grants the preferred number when it is free and the next
    free one otherwise, and remembers the mapping against the id.  Restarting
    both drivers must return the same two numbers, not drift upwards.
    """
    from deye_virtual_battery.venus_bms_publisher import _resolve_instance

    store: dict[str, str] = {}

    class LocalSettings:
        """A localsettings faithful enough to catch drift."""

        def __init__(self, bus, supported, callback):
            path, default, _minimum, _maximum = supported["instance"]
            if path not in store:
                device_class, preferred = parse_class_and_instance(default)
                taken = {
                    parse_class_and_instance(v)[1]
                    for v in store.values()
                    if parse_class_and_instance(v)[0] == device_class
                }
                instance = preferred
                while instance in taken:
                    instance += 1
                store[path] = format_class_and_instance(instance, device_class)
            self._value = store[path]

        def __getitem__(self, key):
            return self._value

    class ReadingBus(FakeBus):
        def get_object(self, service, path):
            if path in store:
                value = store[path]

                class Item:
                    def GetValue(self, dbus_interface=None):
                        return value

                return Item()
            raise RuntimeError("no such setting")

    bus = ReadingBus({})
    args = publisher_args(auto_device_instance=True)

    first = _resolve_instance(
        bus, args, serial="PACK-A", settings_device_factory=LocalSettings
    )
    second = _resolve_instance(
        bus, args, serial="PACK-B", settings_device_factory=LocalSettings
    )
    assert (first.instance, second.instance) == (513, 514)
    assert (first.source, second.source) == (ALLOCATED, ALLOCATED)

    # A restart must not walk the numbers upward: the reservations are found
    # and reused, and nothing is written the second time.
    again_a = _resolve_instance(
        bus, args, serial="PACK-A", settings_device_factory=LocalSettings
    )
    again_b = _resolve_instance(
        bus, args, serial="PACK-B", settings_device_factory=LocalSettings
    )
    assert (again_a.instance, again_b.instance) == (513, 514)
    assert (again_a.source, again_b.source) == (RESERVED, RESERVED)
    assert not again_a.attempted_settings_write
    assert not again_b.attempted_settings_write
    assert sorted(store) == [
        "/Settings/Devices/deye_PACK_A/ClassAndVrmInstance",
        "/Settings/Devices/deye_PACK_B/ClassAndVrmInstance",
    ]
