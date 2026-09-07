"""Victron Venus OS battery driver for Deye SE-F LV packs over BMS-Can.

The package decodes the Deye PCS CAN protocol from a passively observed
SocketCAN interface and publishes a native ``com.victronenergy.battery.*``
D-Bus service, so Venus OS treats the pack as the battery it actually is
instead of misidentifying it as another vendor.

Layers, lowest first:

``candump``/``decoder``
    Parse recorded or live frames and decode them into named fields with
    units, freshness classes and validity flags.
``profile``
    Classify which Deye inverter protocol the battery is transmitting and
    resolve the ``0x356`` current sign accordingly.
``cache``/``policy``
    Hold per-field freshness and turn a decoded snapshot into charge and
    discharge limits, alarms and permissions without performing any action.
``stateful``
    Add debounce, hysteresis and lifecycle state on top of the instantaneous
    policy.
``dbus_model``
    Shape the result as a D-Bus path dictionary, with no D-Bus dependency, so
    the whole pipeline is testable off-device.
``venus_runtime``/``venus_bms_publisher``
    Frame handling and the live publisher that registers on Venus D-Bus.

Only ``venus_bms_publisher`` and ``venus_stage_publisher`` touch D-Bus or a CAN
socket; everything below them operates on plain data.
"""

from .cache import VirtualBatteryCache
from .candump import CanFrame, CandumpParseError, parse_candump_line
from .dbus_model import build_mock_dbus_model
from .decoder import DecodeError, DeyeDecoder
from .policy import PolicyConfig, evaluate_policy
from .profile import DEYE_NATIVE, VICTRON_CAN, ProtocolProfileDetector
from .stateful import StatefulConfig, StatefulShadow
from .version import PACKAGE_VERSION, PROCESS_VERSION, VERSION

__all__ = [
    "CanFrame",
    "CandumpParseError",
    "DEYE_NATIVE",
    "DecodeError",
    "DeyeDecoder",
    "PolicyConfig",
    "ProtocolProfileDetector",
    "StatefulConfig",
    "StatefulShadow",
    "VICTRON_CAN",
    "VERSION",
    "PACKAGE_VERSION",
    "PROCESS_VERSION",
    "VirtualBatteryCache",
    "build_mock_dbus_model",
    "evaluate_policy",
    "parse_candump_line",
]
