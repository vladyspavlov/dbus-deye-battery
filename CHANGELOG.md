# Changelog

Versions are strictly numeric: Venus OS and VRM display `/Mgmt/ProcessVersion`
verbatim, so no suffixes or build tags appear here.

## Unreleased

### Changed
- **The driver is no longer tied to one battery model.** The series cell count
  is measured from the pack and cell voltages, and every pack voltage threshold
  now scales from it instead of being fixed for a 16-series pack. A 16s pack
  produces exactly the previous values (57.6 / 58.4 / 57.2 / 55.2 V), so
  behaviour on an SE-F12-C is unchanged.
- The GX device name is no longer hardcoded to `Deye SE-F12-C`. No Deye pack
  transmits its model designation, so it cannot be detected; set `MODEL=` to
  show your variant, or accept the generic default.

### Added
- `--model` and `--cell-count`, with `MODEL=` and `CELL_COUNT=` in the install
  config. `--cell-count` only applies when the count cannot be measured.
- `/HardwareVersion` reports the measured series count, marked `(assumed)` when
  it had to fall back.

## 0.7.3

### Added
- The pack serial on the standard Venus `/Serial` path, joined from `0x600`
  and `0x650`, registered once at qualification as a fixed path so the update
  loop can never change its D-Bus type.
- `identity.pack_firmware_marker` / `victron_identity.firmware_marker`,
  rendering the version word as the vendor's own `F` designation (`F005`)
  rather than the raw integer `1520`.
- A wiring danger notice in the README. Only Deye PCS pins 4 and 5 may be
  connected, crossed to Victron BMS-Can pins 7 and 8; a straight-through
  patch cable puts CAN levels onto the battery's RS485 pins.
- The Deye Cloud firmware-update procedure, and a note that SE-F5-C and
  SE-F16-C are untested but likely compatible.

## 0.7.2

First public release.

### Added
- VE.Bus service discovery. The service name embeds the port the inverter is
  wired to and differs between GX models, so it is now resolved from D-Bus
  instead of being hard-coded, with rate-limited re-resolution while VE.Bus
  restarts and an explicit `--vebus-service` override.
- Configurable CAN interface, D-Bus service name and device instance, so the
  adapter runs on a GX where BMS-Can is `can1`, and so a second pack can be
  published without colliding. Values are validated rather than merely
  accepted.
- `install/install.sh` and `install/uninstall.sh`, following Victron's driver
  guidance: source under `/data` to survive firmware updates, a daemontools
  service symlinked from `/service`, and a `/data/rc.local` hook.
- `/data/deye-virtual-battery/config`, so the run script no longer has to be
  edited to change the interface or logging.
- Two redacted sample recordings under `tests/data/`, covering the Sol-ark
  profile and a live `Sol-ark` -> `victronCAN` -> `Sol-ark` switch. The
  profile-switch tests now run by default instead of being skipped.

- The pack serial is now published on the standard Venus `/Serial` path. It
  arrives split across `0x600` and `0x650`; both halves must decode as
  printable ASCII before anything is published. Offline tooling masks it by
  default, with `--show-serial` to opt back in.

- `docs/protocol-notes.md`, and reference vectors in
  `tests/test_vendor_app_reference.py` taken from a CAN capture that overlaps a
  timestamped record in the vendor app, so decoder output is pinned against the
  manufacturer's own labels rather than only against our reading of a document.

- `identity.pack_firmware_marker` and `victron_identity.firmware_marker`,
  rendering the version word the way the vendor names its images (`F005`)
  rather than as the raw integer `1520`.

### Changed

- Resolved the `0x550` unit, previously left open: the accumulated counters are
  0.001 kWh. The vendor document states it, and the app's amp-hour view agrees
  exactly at the pack's 51.2 V nominal (6.190 kWh = 120.90 Ah).
- Corrected Deye V3.3 fault tables 2, 3, 4, 6 and 7, the `0x35C` request-heat
  bit, and the `0x35E` manufacturer/pack-number split, all against the
  original-layout protocol tables.
- Profile-aware decoding throughout: the `0x356` current sign, the
  required-freshness set, the alarm source and the charge cross-check all
  follow the detected protocol.
