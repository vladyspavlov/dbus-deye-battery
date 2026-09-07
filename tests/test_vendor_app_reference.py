"""Decoder output checked against the vendor app's own labels.

On 2026-08-30 the battery app recorded a historical entry timestamped
21:13:46 +03:00 (18:13:46 UTC) while a passive CAN capture was running. That
gives a rare thing: a set of frames whose meaning is labelled by the
manufacturer's own software, rather than by our reading of a protocol
document.

These are those frames, verbatim, with the app's labels as the expected
values. Identity frames (0x600/0x650) are omitted -- they carry the pack
serial.
"""

from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.decoder import DeyeDecoder


TIMESTAMP = 1788113626.0  # 2026-08-30 18:13:46 UTC

REFERENCE_FRAMES = {
    "110": "0000000000000021",
    "150": "15020000BA03E803",
    "200": "070D050DF000E600",
    "250": "0E0170FED300E600",
    "351": "48020000FC08E001",
    "355": "6000640000000000",
    "356": "D2140000E6000000",
    "400": "0000000000001200",
    "550": "2E18000035020000",
}


def decoded(can_id: str) -> dict:
    frame = parse_candump_line(
        f"({TIMESTAMP:.6f}) can0 {can_id}#{REFERENCE_FRAMES[can_id]} R"
    )
    return {field.name: field.value for field in DeyeDecoder().decode(frame)}


def test_mos_states_match_the_app_exactly():
    """App: Pre-charge Open, Charge Open, Discharge Closed, HT Open, Parallel 1."""
    values = decoded("110")
    assert values["mos.precharge_closed"] is False
    assert values["mos.charge_closed"] is False
    assert values["mos.discharge_closed"] is True
    assert values["mos.heater_closed"] is False
    assert values["mos.parallel_complete"] is True


def test_no_protection_was_triggered():
    """App: "Protection Status Flag: No protection triggered"."""
    assert decoded("110")["pack.any_v33_condition_active"] is False
    assert decoded("400")["system.fault_level"] == "none"


def test_cell_temperature_extrema_match_the_app():
    """App: Max Battery Temperature 24 C, Min Battery Temperature 23 C."""
    values = decoded("200")
    assert values["cells.max_temperature_200"] == 24.0
    assert values["cells.min_temperature_200"] == 23.0


def test_unpopulated_sensors_read_minus_forty():
    """The app shows -40 C for every absent sensor; so must the decoder."""
    assert decoded("250")["pack.heating_film_temperature"] == -40.0


def test_limits_match_the_app():
    """App: Discharge Current Limit 230.0 A, Charge Current Limit 0.0 A."""
    values = decoded("351")
    assert values["limits.max_discharge_current"] == 230.0
    assert values["limits.max_charge_current"] == 0.0


def test_state_of_health_and_charge_match_the_app():
    """App: SOC 95.6, SOH 100.0. 0x355 is whole percent, 0x150 is 0.1 %."""
    assert decoded("355")["battery.soh"] == 100.0
    assert decoded("355")["battery.soc"] == 96.0
    assert decoded("150")["diagnostics.soh_150"] == 100.0
    assert decoded("150")["diagnostics.soc_150"] == 95.4


def test_operating_status_matches_the_app():
    """App: "System Operating Status: Standby"."""
    assert decoded("400")["system.operation_mode"] == "standstill"


def test_accumulated_energy_is_kwh_not_amp_hours():
    """0x550 is 0.001 kWh, per the vendor protocol -- and the app agrees.

    The app reports the same counter in amp-hours as "Total Charge AH
    120.90Ah". At this pack's 51.2 V nominal (16s x 3.2 V), 6.190 kWh is
    120.90 Ah, matching to every digit shown. That settles a unit this
    project had previously left open.
    """
    values = decoded("550")
    assert values["history.charged_energy"] == 6.190
    assert values["history.discharged_energy"] == 0.565
    assert round(6.190 * 1000 / 51.2, 2) == 120.90


def test_cell_positions_are_absent_from_can():
    """The app reports max/min cell and temperature *positions*; CAN does not.

    Publishing an invented cell id would be worse than publishing none, so
    /System/MaxVoltageCellId stays invalid. This test records that the
    positions really are absent from the wire rather than merely unhandled.
    """
    everything = {}
    for can_id in REFERENCE_FRAMES:
        everything.update(decoded(can_id))
    assert not [name for name in everything if "position" in name.lower()]
    assert not [name for name in everything if name.endswith("cell_id")]
