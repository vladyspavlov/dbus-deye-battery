"""Plain-data simulation of selected Venus/systemcalc battery interactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VenusScenario:
    """A hypothetical configuration; it never reads or writes Venus settings."""

    name: str
    controlling_bms: str = "none"
    battery_monitor: str = "multiplus"
    dvcc_enabled: bool = True


CURRENT_NO_BMS_SCENARIO = VenusScenario(
    name="current-no-bms-control",
    controlling_bms="none",
    battery_monitor="multiplus",
)

HYPOTHETICAL_VIRTUAL_BMS_SCENARIO = VenusScenario(
    name="hypothetical-virtual-bms-selected",
    controlling_bms="virtual",
    battery_monitor="virtual",
)


def simulate_systemcalc(
    stateful_model: dict[str, Any],
    *,
    scenario: VenusScenario,
) -> dict[str, Any]:
    """Return what a constrained mock systemcalc consumer would see.

    This is intentionally a conservative behavioral model, not an assertion
    that every installed Venus version implements identical selection details.
    """

    paths = stateful_model["paths"]
    service_name = "com.victronenergy.battery.deye_se_f12"
    would_exist = bool(stateful_model["would_register_mock_service"])
    connected = bool(paths.get("/Connected"))
    virtual_selected = scenario.controlling_bms == "virtual"
    active = virtual_selected and would_exist and connected
    bms_updates_stopped = virtual_selected and would_exist and not active
    bol_timeout_expected = bool(
        paths.get("/Diagnostics/Lifecycle/VebusBolTimeoutExpected")
    )
    # The alarm comes from the Multi after its BOL communication timeout; it
    # is not asserted immediately by systemcalc when the battery service is
    # removed.  The installed Venus 3.75 code stops BOL writes on loss, and
    # the official VE.Bus protocol specifies a 300-second timeout.
    bms_lost = bms_updates_stopped and bol_timeout_expected
    consumes_limits = active and scenario.dvcc_enabled

    if scenario.battery_monitor == "virtual" and active:
        battery_monitor_service = service_name
        system_voltage = paths.get("/Dc/0/Voltage")
        system_soc = paths.get("/Soc")
    elif scenario.battery_monitor == "multiplus":
        battery_monitor_service = "com.victronenergy.vebus.mock"
        system_voltage = None
        system_soc = None
    else:
        battery_monitor_service = None
        system_voltage = None
        system_soc = None

    return {
        "mode": "pc-only-mock-systemcalc",
        "scenario": scenario.name,
        "action_taken": False,
        "registered_on_dbus": False,
        "writes_settings": False,
        "writes_vebus_mode": False,
        "model_fidelity": (
            "installed Venus 3.75 selection/DVCC path plus official "
            "300-second VE.Bus BOL timeout"
        ),
        "service_discovered": would_exist,
        "active_bms_service": service_name if active else None,
        "active_bms_instance": paths.get("/DeviceInstance") if active else None,
        "control_bms_parameters": int(consumes_limits),
        "bms_connection_lost_alarm": int(bms_lost),
        "battery_monitor_service": battery_monitor_service,
        "system_voltage_v": system_voltage,
        "system_soc_percent": system_soc,
        "dvcc_inputs": {
            "max_charge_voltage_v": paths.get("/Info/MaxChargeVoltage") if consumes_limits else None,
            "max_charge_current_a": paths.get("/Info/MaxChargeCurrent") if consumes_limits else None,
            "max_discharge_current_a": paths.get("/Info/MaxDischargeCurrent") if consumes_limits else None,
        },
        "risk_observations": {
            "selected_bms_unavailable": bms_updates_stopped,
            "vebus_bol_updates_stopped": bms_updates_stopped,
            "vebus_bol_timeout_remaining_seconds": paths.get(
                "/Diagnostics/Lifecycle/VebusBolTimeoutRemaining"
            ),
            "bms_loss_shutdown_pending": bms_updates_stopped and not bms_lost,
            "bms_loss_timeout_elapsed": bms_lost,
            "bms_loss_may_inhibit_charge_or_discharge": bms_lost,
            "ac_continuity_not_guaranteed_if_selected_bms_is_lost": (
                bms_updates_stopped
            ),
            "would_directly_switch_inverter_off": False,
        },
    }
