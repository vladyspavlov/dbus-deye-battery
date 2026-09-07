# Protocol notes

Findings that did not fit in the README, and the questions still open.
Confidence is stated for each: *confirmed* means checked against the vendor's
own software or protocol document, *probable* means one consistent
observation, *unknown* means exactly that.

## Cross-validation against the vendor app

On 2026-08-30 the battery app recorded a historical entry timestamped
`21:13:46 +03:00` (18:13:46 UTC) while a passive CAN capture was running.
That yields frames whose meaning is labelled by the manufacturer's own
software rather than by our reading of a document. Those frames are locked
into `tests/test_vendor_app_reference.py` as regression vectors.

| App label | App value | Decoded field | Value | |
|---|---|---|---|---|
| Pre-charge MOS Status | Open | `mos.precharge_closed` | `False` | ✅ |
| Charge MOS Status | Open | `mos.charge_closed` | `False` | ✅ |
| Discharge MOS Status | Closed | `mos.discharge_closed` | `True` | ✅ |
| HT MOS Status | Open | `mos.heater_closed` | `False` | ✅ |
| Parallel Flag | 1 | `mos.parallel_complete` | `True` | ✅ |
| Protection Status Flag | No protection triggered | `pack.any_v33_condition_active` | `False` | ✅ |
| System Operating Status | Standby | `system.operation_mode` | `standstill` | ✅ |
| Max Battery Temperature | 24 °C | `cells.max_temperature_200` | `24.0` | ✅ |
| Min Battery Temperature | 23 °C | `cells.min_temperature_200` | `23.0` | ✅ |
| Discharge Current Limit | 230.0 A | `limits.max_discharge_current` | `230.0` | ✅ |
| Charge Current Limit | 0.0 A | `limits.max_charge_current` | `0.0` | ✅ |
| SOH(%) | 100.0 | `battery.soh` | `100.0` | ✅ |
| SOC | 95.6 | `battery.soc` / `diagnostics.soc_150` | `96` / `95.4` | ~ |
| Total Voltage | 53.2 V | `diagnostics.voltage_150` | `53.3` | ~ |

The two `~` rows are sampling differences, not decode errors: the app and the
CAN bus sample independently. `0x355` carries whole-percent SOC and the Deye
`0x150` frame carries 0.1 %; the driver publishes the `0x355` value on `/Soc`
because that is the frame Venus's protocol contract defines, and keeps the
finer one as a diagnostic.

## Resolved: `0x550` is energy, not charge

**Confirmed.** The accumulated counters in `0x550` are `uint32` in units of
0.001 kWh. This had been left open because the field could plausibly have been
amp-hours.

Two independent confirmations:

- the vendor protocol document states *"Accumulated charge capacity ... Unit:
  0.001Kwh Unsigned int32"*;
- the app reports the same counter in amp-hours. At the reference sample CAN
  read `6190`, and the app showed `Total Charge AH 120.90Ah`. This pack is
  16s, so nominal is 51.2 V, and 6.190 kWh ÷ 51.2 V = **120.90 Ah** — every
  digit the app displays.

The discharge pair does not line up as cleanly (565 Wh ÷ 51.2 V = 11.04 Ah
against a displayed 10.04 Ah). The most likely explanation is that the app
record and the CAN sample are from slightly different instants, since the
counters only ever increase. Worth revisiting if a simultaneous pair is ever
captured.

## Confirmed absent from CAN

The app displays these; the PCS CAN bus does not carry them. The driver
publishes Venus's invalid value rather than inventing one — notably
`/System/MaxVoltageCellId` and `/System/MinVoltageCellId` stay unset.

- maximum and minimum cell voltage **position**, and the temperature-sensor
  positions
- `Remaining Capacity` in Ah
- `AFE Temperature`, terminal B+/B-/P and connector temperatures
- `BMS LIFE Value`
- the full software and hardware version strings (`LVESS...`); CAN carries
  only the short marker described below

## Firmware marker in `0x500` and `0x363`

**Probable.** `0x500` bytes 0-1 changed `F0 02` → `F0 05` across a firmware
update, and `0x363` bytes 0-1 carry the same word. Rendered as hex digits
these read `F002` and `F005`, and `F005` is the suffix on the installed image
name `LVESS1526701N01_F005`.

Only one pairing can be checked, because the older image name carries no `F`
suffix. Treat it as a marker to diff against your own earlier captures, not as
a decoder for the vendor's version string.

## Open: `0x400` system status enumeration

**Unknown.** `system.substate_raw` is published as an opaque number and is
never used as a control input.

At the reference sample `0x400` byte 6 read `0x12` (18) while the app showed
`System Status 2` and `System Sub-status 3`, with `System Operating Status:
Standby`. Reading the byte as two nibbles gives 1 and 2, one below each of the
app's numbers, which would fit a 0-based wire value displayed 1-based — but
that is a single data point and two coincidences, so it is recorded as an
observation, not a mapping.

Values observed on the wire so far: `0x0003`, `0x0008`, `0x0010`, `0x0011`,
`0x0012`.

## Open: the fifth MOS

**Unknown.** The app shows a `Current Limit MOS Status` alongside pre-charge,
charge, discharge and heating. `0x110` byte 7 bits 1-3 are unassigned by the
protocol document and are the obvious candidates, but that MOS has read `Open`
in every sample captured, so the bit cannot be identified. The bits are kept
as `mos.reserved_flags`.

A capture taken while the pack is actively current-limiting would settle it.
