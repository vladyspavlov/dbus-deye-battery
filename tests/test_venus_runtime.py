import struct

import pytest

from deye_virtual_battery.venus_runtime import (
    CAN_ERR_FLAG,
    CAN_FRAME_FORMAT,
    CAN_RTR_FLAG,
    RuntimeClock,
    SocketCanFrameError,
    VenusPublisherCore,
    decode_socketcan_frame,
    encode_socketcan_frame,
)
from deye_virtual_battery.venus_stage_publisher import _add_paths
from deye_virtual_battery.venus_bms_publisher import _add_paths as _add_bms_paths
from deye_virtual_battery.version import VERSION
from deye_virtual_battery.policy import PolicyConfig


def raw_frame(can_id: int, payload_hex: str) -> bytes:
    payload = bytes.fromhex(payload_hex)
    return struct.pack(CAN_FRAME_FORMAT, can_id, len(payload), payload.ljust(8, b"\0"))


def sequence(values):
    iterator = iter(values)
    return lambda: next(iterator)


def test_runtime_clock_progresses_across_backward_wall_clock_step():
    clock = RuntimeClock(
        wall_time=sequence((1000.0, 1001.0, 900.0, 901.0, 2000.0, 1900.0)),
        monotonic_time=sequence((10.0, 11.0, 12.0, 13.0, 14.0, 15.0)),
    )

    assert clock.now() == 1001.0
    assert clock.now() == 1002.0
    assert clock.now() == 1003.0
    assert clock.now() == 2000.0
    assert clock.now() == 2001.0


def apply_cycle(core: VenusPublisherCore, timestamp: float) -> None:
    frames = (
        (0x110, "0000000000000021"),
        (0x200, "2D0D080DE600E600"),
        (0x351, "48020000FC08E001"),
        (0x355, "6000640000000000"),
        (0x356, "D2140000E6000000"),
        (0x359, "0000000000000000"),
        (0x361, "2D0D080DE600E600"),
        (0x371, "0000FC0800000000"),
    )
    for index, (can_id, payload) in enumerate(frames):
        core.apply_raw_frame(
            raw_frame(can_id, payload),
            timestamp=timestamp + index * 0.01,
        )


def test_socketcan_classical_frame_is_decoded_without_claiming_direction():
    frame = decode_socketcan_frame(
        raw_frame(0x356, "D2140E00DC000000"),
        timestamp=1000.0,
    )
    assert frame.can_id == 0x356
    assert frame.payload == bytes.fromhex("D2140E00DC000000")
    assert frame.direction is None
    assert frame.timestamp == 1000.0


def test_socketcan_keepalive_encoding_is_exact():
    raw = encode_socketcan_frame(0x307, bytes.fromhex("1234567856494300"))
    can_id, dlc, payload = struct.unpack(CAN_FRAME_FORMAT, raw)
    assert can_id == 0x307
    assert dlc == 8
    assert payload == bytes.fromhex("1234567856494300")


@pytest.mark.parametrize("flag", [CAN_RTR_FLAG, CAN_ERR_FLAG])
def test_socketcan_control_and_error_frames_are_rejected(flag):
    with pytest.raises(SocketCanFrameError):
        decode_socketcan_frame(raw_frame(flag | 0x351, ""), timestamp=1000.0)


def test_runtime_core_qualifies_and_preserves_no_shutdown_policy():
    core = VenusPublisherCore()
    apply_cycle(core, 1000.0)
    first = core.step(timestamp=1000.2, vebus_voltage_v=53.3)
    assert first["lifecycle"]["state"] == "starting"

    apply_cycle(core, 1002.2)
    online = core.step(timestamp=1002.3, vebus_voltage_v=60.78)
    assert core.qualified is True
    assert online["paths"]["/ProductId"] == 0xFFFF
    assert online["paths"]["/Info/MaxChargeCurrent"] == 0.0
    assert online["paths"]["/Info/MaxDischargeCurrent"] == 230.0
    assert online["paths"]["/Diagnostics/Alarms/VoltageDisagreement"] == 2
    assert online["instantaneous_policy"]["permissions"][
        "would_directly_switch_inverter_off"
    ] is False


def test_stage_service_is_measurement_only_and_cannot_be_selected_as_bms():
    class FakeService:
        def __init__(self):
            self.paths = {}

        def add_path(self, path, value):
            self.paths[path] = value

    core = VenusPublisherCore()
    apply_cycle(core, 1000.0)
    core.step(timestamp=1000.2, vebus_voltage_v=53.3)
    apply_cycle(core, 1002.2)
    model = core.step(timestamp=1002.3, vebus_voltage_v=53.3)
    service = FakeService()
    dynamic = _add_paths(service, model["paths"], duration_seconds=120.0)

    assert service.paths["/ProductId"] == 0xFFFF
    assert service.paths["/Diagnostics/Stage/MeasurementOnly"] == 1
    assert "/Dc/0/Voltage" in service.paths
    assert not any(path.startswith("/Info/") for path in service.paths)
    assert not any(path.startswith("/Io/") for path in service.paths)
    assert not any(path.startswith("/Info/") for path in dynamic)


def test_commissioning_service_is_neutral_selectable_and_keeps_deye_limits():
    class FakeService:
        def __init__(self):
            self.paths = {}

        def add_path(self, path, value):
            self.paths[path] = value

    core = VenusPublisherCore()
    apply_cycle(core, 1000.0)
    core.step(timestamp=1000.2, vebus_voltage_v=53.3)
    apply_cycle(core, 1002.2)
    model = core.step(timestamp=1002.3, vebus_voltage_v=53.3)
    service = FakeService()
    dynamic = _add_bms_paths(service, model["paths"], config=PolicyConfig())

    assert service.paths["/ProductId"] == 0xFFFF
    assert service.paths["/CustomName"] == "Deye SE-F12-C"
    assert service.paths["/Info/MaxChargeVoltage"] == 55.2
    assert service.paths["/Info/MaxChargeCurrent"] == 0.0
    assert service.paths["/Info/MaxDischargeCurrent"] == 230.0
    assert service.paths["/Capacity"] == 230.0
    assert service.paths["/ConsumedAmphours"] == 9.2
    assert service.paths["/System/MaxVoltageCellId"] is None
    assert service.paths["/System/MinVoltageCellId"] is None
    assert service.paths["/Capabilities/ChargeVoltageControl"] == 0
    # The published version is strictly numeric: Venus and VRM display
    # /Mgmt/ProcessVersion verbatim.
    assert service.paths["/Mgmt/ProcessVersion"] == VERSION
    assert VERSION.replace(".", "").isdigit()
    assert service.paths["/Diagnostics/Safety/AlarmFlagsControlLimits"] == 0
    assert service.paths["/Diagnostics/Commissioning/NoSettingsWrites"] == 1
    assert service.paths["/Diagnostics/Safety/RuntimeClock"] == "monotonic-anchored-wall"
    assert service.paths["/Diagnostics/Publisher/Heartbeat"] == 0
    assert service.paths["/UpdateIndex"] == 0
    assert "/Info/MaxChargeVoltage" in dynamic
    assert "/Diagnostics/Commissioning/Selected" in dynamic
    assert "/Diagnostics/Publisher/Heartbeat" in dynamic
    assert "/UpdateIndex" in dynamic
