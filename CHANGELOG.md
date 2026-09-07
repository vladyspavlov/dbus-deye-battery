# Changelog

Versions are strictly numeric: Venus OS and VRM display `/Mgmt/ProcessVersion`
verbatim, so no suffixes or build tags appear here.

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

### Changed
- Corrected Deye V3.3 fault tables 2, 3, 4, 6 and 7, the `0x35C` request-heat
  bit, and the `0x35E` manufacturer/pack-number split, all against the
  original-layout protocol tables.
- Profile-aware decoding throughout: the `0x356` current sign, the
  required-freshness set, the alarm source and the charge cross-check all
  follow the detected protocol.
