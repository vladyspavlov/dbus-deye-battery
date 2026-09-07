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

## Firmware marker in `0x500`, `0x363` and `0x35F`

**Confirmed.** The two-byte version word rendered as hex digits is the vendor's
own `F` designation. `F0 05` reads `F005`, and the image installed on the
reference pack is `LVESS1526701N01_F005`. Before the update the wire read
`F0 02`.

Published as `identity.pack_firmware_marker` (and
`victron_identity.firmware_marker` from `0x35F`), because `F005` is what the
vendor's tooling shows, whereas the raw little-endian integer for the same
bytes is `1520`.

The same word appears in three frames — `0x500` bytes 0-1, `0x363` bytes 0-1
and `0x35F` bytes 2-3 — so any one of them identifies the running firmware
from a capture alone.

The rest of the image name (`LVESS15…N01`) is not on the CAN bus. Read as a
build date, `25814` → 2025-08-14 and `26701` → 2026-07-01 would fit the two
observed names, but that is a guess from two samples and nothing depends on
it.

## `DY` is Deye

**Confirmed.** The vendor protocol defines `0x35E` bytes 0-1 as the
manufacturer name, spelled out in the document as *DEYE*, in ASCII. `DY` is
that abbreviation.

The same two characters appear at the end of `0x35F` (bytes 6-7), which had
been recorded as an undocumented suffix. `0x35F` turns out to be the same
identity data rearranged:

```
0x35E   44 59 | 30 30 31 | 1C    | FC 08     "DY"  "001"  cell 0x1C  2300
0x35F   00 1C | F0 05    | FC 08 | 44 59     cell 0x1C  F005  2300  "DY"
```

## Open: cell manufacturer code `0x1C`

**Unknown.** `0x35E` byte 5 reads `0x1C` (28) on every frame captured — 337
identical `0x35E` payloads, with no variation. The low byte of `0x35F` carries
the same value, so it is a real identity field rather than padding, and the
byte assignment is right.

It cannot be resolved from the documentation available: the V3.3 protocol
lists only `1 = GOTION`, `2 = CATL`, `3 = EVE`. 28 is not in that list, and no
other cell-vendor name appears anywhere in the document. Either the list has
grown since V3.3 or this firmware encodes it differently.

Settling it needs a newer protocol revision, or the vendor confirming which
cells an SE-F12-C contains. Until then it stays a numeric diagnostic.

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
