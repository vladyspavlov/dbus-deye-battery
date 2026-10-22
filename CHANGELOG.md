# Changelog

Versions are strictly numeric: Venus OS and VRM display `/Mgmt/ProcessVersion`
verbatim, so no suffixes or build tags appear here.

## 0.8.2

### Fixed
- **The driver used far more CPU than it needed.** On a GX it kept about a
  third of one core busy, and since 0.8.0 noticeably more with a parallel
  bank ([#10](https://github.com/vladyspavlov/dbus-deye-battery/issues/10)).
  Most of it was the CAN keepalive thread: every 50 ms it walked all of
  `/proc` to see whether the stock `can-bus-bms` driver was running, about
  13 ms of work each time on a GX with 300 processes, even on installations
  where it never transmits. It now follows the stock driver by its pid while
  it runs and looks for it at most once a second while it is absent, and
  still looks again right before every keepalive it sends. Each 250 ms update
  also does about a third less work: timestamps are no longer formatted as
  text for every field four times a second, and the 30 or so pack slots no
  pack uses are no longer rebuilt. Nothing that is published changes.

## 0.8.1

### Fixed
- **An all-zero `0x351` from a parallel bank blocked discharge and held CVL
  down** ([#7](https://github.com/vladyspavlov/dbus-deye-battery/issues/7)).
  A two-pack SE-F5 Pro-C bank sends an all-zero `0x351` between its real ones,
  every 5.35 s. Its 0 V voltage limits were already refused, but its 0 A
  currents were taken as a request: the driver published DCL 0 A for about a
  second at a time, which can stop an inverter running off-grid, and while
  charging it held CVL at the blocked-charge 55.2 V, because the 20 s
  qualification for raising it never completed between two such frames. A
  `0x351` whose own charge voltage or low voltage limit is out of range is
  now refused as a whole, and the previous limits stay in place. If such
  frames are all that arrives, the limits age out after 3 s exactly as when
  `0x351` stops. A real stop, with valid voltage limits and 0 A, is followed
  as before.

### Added
- `/Diagnostics/Deye/RejectedLimitFrames`: the number of `0x351` frames whose
  limits were refused.

## 0.8.0

Multi-pack support. Deye packs linked by their parallel cable present one bank
on the master's PCS port; until now the driver read only pack 1's frames and
treated them as the whole bank.

### Fixed
- **A full pack blocked charging of the whole bank**
  ([#6](https://github.com/vladyspavlov/dbus-deye-battery/issues/6)). With two
  SE-F5 Pro-C in parallel, pack 1 at 97.6 % opened its charge MOS while pack 2
  at 90.5 % kept accepting charge, and the bank asked for 45 A in `0x351`. The
  driver saw only pack 1's MOS and published CCL 0 A. Charge is now allowed
  while any pack's charge MOS is closed, and blocked only when every pack
  blocks it. A single pack behaves exactly as before.
- **The pack charge ceiling capped a bank at one pack's share.** The `0x250`
  cross-check applied pack 1's own allowable current to the whole bank (18 A
  + 1 in that capture, against 45 A requested). It is now the sum over the
  packs that accept charge, and is skipped while a pack's frames are missing.
  The bank's `0x351` request is still never exceeded.
- **The current-sign cross-check could inhibit charge on a healthy bank.** It
  compared the bank current in `0x356` with pack 1's current alone; parallel
  packs can legitimately carry opposite currents. It now compares with the sum
  of every pack.
- **Faults on any pack other than pack 1 raised no alarm** from the per-pack
  condition tables or `0x400` fault level. Every pack's conditions now map to
  Venus alarms, and the worst pack's fault level is the bank's.
- Charged/discharged energy and the cycle count showed pack 1 only. Energy is
  now summed across packs; the cycle count is the most-cycled pack's.
- `/System/NrOfCellsPerBattery` was a hard-coded 16; it is now the measured
  series count.

### Added
- Every per-pack frame (`0x110`, `0x150`, `0x200`, `0x250`, `0x400`, `0x500`,
  `0x550`, `0x600`, `0x650`, `0x700`, `0x750` + pack − 1, up to 32 packs) is
  decoded under `packs.<n>.`. Pack 1 keeps its original field names.
- `/System/NrOfBatteries` and `/System/BatteriesParallel` publish the number of
  packs, and `/System/BatteriesSeries` is 1, so the battery's **System** page
  in the GX shows the bank layout. `/Balancing` reports whether any pack is
  balancing.
- `/Battery/<n>/Voltage`, `Current`, `Soc`, `Temperature` and `Serial` for each
  pack, with a writable `/Battery/Request/Id` so gui-v2's **Individual battery
  info** page can switch between packs. It only selects what is displayed.
- `/Diagnostics/Pack/<n>/` (online, charge/discharge MOS, allowable currents,
  cell extremes) and `/Diagnostics/Bank/` (pack count, packs online, whether the
  bank is complete, bank charge MOS).
- Per-pack serials are redacted from offline tool output like the bank serial.

### Unchanged
- Capacity is the bank's, straight from `0x35E` (two 100 Ah packs report
  200 Ah); it is not summed again.
- Replays of the reference SE-F12-C and SE-G5.1 Pro B captures, in both
  battery protocol profiles, publish the same values and take the same
  decisions as 0.7.10; only the new paths are added.

## 0.7.10

### Fixed
- **A `MODEL=` value containing a space was silently dropped**
  ([#1](https://github.com/vladyspavlov/dbus-deye-battery/issues/1)).
  `install/bootstrap.sh` seeded the config unquoted, and `service/run`
  sources it, so `MODEL=SE-F5 Pro-C` ran `Pro-C` as a command and the GX
  showed the generic name. Seeded values are now single-quoted. An existing
  config is never rewritten on upgrade: if you set such a model by hand,
  quote it yourself.
- `tools/validate_real_captures.py` compared capacity against a hard-coded
  230 Ah and ignored `--capacity-ah`, so any other pack size failed.

### Compatibility
- **Deye SE-F5 Pro-C** confirmed on hardware by a user: zero decode errors on
  the `deye_native` frame set, 100 Ah and 16 cells read from the wire.
- SE-F5-C removed from the documentation; it does not appear to be a real
  model designation.

## 0.7.9

### Fixed
- **`handoff-can-owner` could never complete on a fresh install**
  ([#2](https://github.com/vladyspavlov/dbus-deye-battery/issues/2)). It
  refused unless the driver reported `/Diagnostics/CanTx/FeatureEnabled = 1`,
  but the driver set that once at startup from the
  `/data/deye-virtual-battery/can-tx-enabled` marker, and the only thing that
  creates the marker is a successful handoff. So the handoff always stopped
  with *"virtual BMS is not selected, fresh, progressing, and TX-capable"*,
  and the stock `can-bus-bms` driver kept publishing its misidentified
  LG battery service. It failed safe: nothing was armed and the stock driver
  kept the keepalive.

  The driver now polls the marker (`--can-tx-enable-file`) instead of reading
  it once, and `FeatureEnabled` is published live. The handoff checks its
  other preconditions, creates the marker, waits for the running driver to
  report the feature enabled, and only then arms and stops the stock driver.
  If the driver never reports it (for example an older driver), the handoff
  removes the marker and refuses without touching the stock driver. Transmission
  still needs all three of the feature, the runtime arm file and a proven-absent
  stock driver. The TX socket is now opened on first ownership rather than at
  startup.

## 0.7.8

### Fixed
- **`handoff-can-owner` aborted on every busybox GX.** Its VE.Bus discovery
  piped through `head -1`, which GNU coreutils accepts but the busybox `head`
  on Venus OS rejects (`invalid option -- '1'`). Under `set -e` the handoff
  died in `discover_vebus` before arming anything — so it failed safe (the
  stock driver kept the keepalive), but the CAN handover could never complete
  on a real device. Now `head -n 1`. This shipped in `install/`, so it is
  carried into the release rather than left on `main`.

### Verified
- **Second model confirmed on hardware: Deye SE-G5.1 Pro B** (100 Ah, 16
  series) — a different series and capacity from the SE-F12-C. It decodes with
  zero errors on the same `deye_native` frame set, and the driver runs live as
  its controlling BMS with capacity and series count measured from the pack.
  No code was model-specific; see [Compatibility](README.md#compatibility-and-tested-scope).

## 0.7.7

### Fixed
- **The series cell count was never actually measured on a real pack.** The
  count was accepted only if it reconstructed the pack voltage to within a
  fixed 0.25 V of the mean of the two extreme cells — but that mean is a
  biased estimator whenever the cells are unevenly spread. On the reference
  16s pack (54.5 V, cells at 3.352 and 3.524) the bias is 0.51 V, so the
  correct count was rejected and every pack quietly fell back to the assumed
  16. The check is now that the per-cell voltage the count implies lies inside
  the measured min/max bracket, which is true of the real count by definition
  and which the neighbouring counts fail.

  No threshold moves on a 16s pack — 57.6 / 58.4 / 57.2 / 55.2 either way —
  but a pack that is not 16s now gets its own thresholds instead of a 16s
  pack's.
- **`--device-instance` was ignored.** `/DeviceInstance` was published from a
  module constant, so two packs configured with different instances both
  announced 513 — exactly the collision the option exists to prevent. The
  staged publisher had the same bug. The argument is now honoured, and the
  accepted range is VRM's own 0–32767 rather than 0–9999.
- The fake-GX test harness required `git` to build its fixture, so the suite
  failed rather than skipped when run from an unpacked release — which is
  exactly how a release gets checked. It now falls back to `find`, and a test
  runs the harness against a copy of the tree with no `.git` to keep it that
  way. Same for shadowing `curl` on an image that has none.

### Added
- **Venus resolves the device instance.** A VRM device instance has to be
  unique per device class, and 513 was only ever safe against one arrangement:
  the stock `can-bus-bms` driver takes 512 on `can0`. Where BMS-Can is `can1`
  the stock driver may take 513 itself, and two packs running this driver
  collided outright.

  With `AUTO_DEVICE_INSTANCE=1` — the default for a fresh install — the driver
  reserves its instance through localsettings at
  `/Settings/Devices/<id>/ClassAndVrmInstance`, keyed on the pack serial and
  falling back to the interface name. That is the mechanism Victron documents
  and what the stock driver itself does. Two packs come up as 513 and 514 with
  no configuration.

  It is guarded, because moving `/DeviceInstance` deselects the battery
  monitor and splits VRM history. An instance the system currently selects is
  never moved — localsettings is not even asked. An existing reservation is
  reused rather than replaced. Every failure falls back to the configured
  number. `install.sh` writes `AUTO_DEVICE_INSTANCE=0` when upgrading an
  install made before the option existed, the same way it already pins
  `SERVICE_NAME`.

  New diagnostics say what happened: `/Diagnostics/Instance/Source`
  (`configured`, `pinned-to-selection`, `reserved`, `allocated`, `fallback`),
  `/Diagnostics/Instance/SettingsId` and `/Diagnostics/Instance/Requested`.
  `/Diagnostics/Commissioning/NoSettingsWrites` now reports what the running
  process actually did rather than a constant `1`.

- `tools/preflight.sh` — runs what CI runs, before pushing: shell syntax for
  every script found by its shebang (CI's own job now calls the same entry
  point, so the list cannot drift), then the suite with
  `GX_SANDBOX_REQUIRED=1`. `--matrix` additionally runs the suite on the
  oldest and newest supported Python in Docker, which is where a difference
  between a developer's machine and the runner actually shows up.

## 0.7.6

### Added
- **A one-command install.** `install/bootstrap.sh`, run as

  ```sh
  wget -qO- https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/main/install/bootstrap.sh | sh
  ```

  resolves the latest release, downloads it, verifies it, detects the CAN port
  and installs it. `VERSION`, `CAN_INTERFACE`, `MODEL`, `DEVICE_INSTANCE`,
  `SERVICE_NAME`, `SHA256`, `ALLOW_DOWNGRADE` and `DRY_RUN` are read from the
  environment, because a piped script cannot take arguments. It still changes
  no system behaviour: selecting the driver and handing over CAN ownership
  remain separate, deliberate steps.

  What it refuses to do matters as much as what it does. It will not install a
  tag whose packaged version disagrees with it, will not install over a newer
  version without `ALLOW_DOWNGRADE=1`, will not proceed on a failed `SHA256`,
  and will not run anywhere that is not a GX. Every action lives in a function
  with `main "$@"` as the last line, so a truncated download does nothing at
  all. On an upgrade it leaves the existing config untouched, since silently
  moving a working system to another CAN port or D-Bus identity would deselect
  it as battery monitor.

- **`install/detect-can-interface.sh`** — works out which port the battery is
  on instead of assuming `can0`, from five signals: which `can*` interfaces
  exist, link state and bitrate, the Venus `/Settings/Canbus/<if>/Profile`
  setting (`3` is CAN-bus BMS LV at 500 kbit/s), which interface the stock
  `can-bus-bms.<if>` service was started on, and finally the Deye frames
  themselves. Read-only: it never brings an interface up or down, changes a
  setting, or transmits. `install.sh` now leaves it at
  `/data/deye-virtual-battery/detect-can-interface.sh`, a stable path.

- **`tests/gxsim/gx-sandbox.sh`** — builds a throwaway fake Venus OS with
  bubblewrap, with `dbus`, `candump`, `ip` and `svc` stubs answering values
  captured from a real GX. `tests/test_installer.py` runs the real bootstrap
  inside it and checks the fresh install, the upgrade path, the downgrade
  refusal, a bad checksum, a mislabelled tag, the BusyBox-wget-only path, being
  piped into a shell, and `DRY_RUN`. It also asserts the installer only ever
  starts its own service and only ever reads from D-Bus. No inverter is
  involved at any point. CI installs bubblewrap so these do not silently skip.

### Changed
- The README installation section leads with the one-command install, explains
  how to read and pin the script before running it, and gains a section on
  finding the BMS-Can interface with each check spelled out to run by hand.
  Both are mirrored in the Ukrainian README. Numbered step headings were
  dropped, so their anchors are now named rather than numbered.

## 0.7.5

### Changed
- **The project is now named `dbus-deye-battery` throughout**, matching the
  repository and Victron's naming convention for Venus OS D-Bus drivers. The
  distribution name, the README title and the package description all agree.
- **The default D-Bus service name is now model-neutral**:
  `com.victronenergy.battery.deye_lv`, was `...deye_se_f12`. The driver is not
  tied to an SE-F12. Upgrading cannot change a running system's identity: the
  installer pins the previous name into the config of any existing install that
  never set one, because a changed service name deselects it as battery monitor
  and BMS.
- The default GX device name and the offline shadow's `/ProductName` are now
  `Deye LV battery` rather than a fixed model. Set `MODEL=` for your variant.
- The package description everywhere is "Victron Venus OS battery driver for
  Deye SE-F LV packs over BMS-Can".

### Added
- **A Ukrainian translation of the README** ([`README.uk.md`](README.uk.md)),
  cross-linked with the English original, which remains authoritative.
- README badges, a table of contents, an FAQ, and `[project.urls]` metadata.
- `tests/test_repository_metadata.py`: the distribution name matches the
  repository, one description is used everywhere, both READMEs pin the current
  version and link to each other, every relative link and every table-of-
  contents anchor resolves, and no model-specific service name survives outside
  the installer's deliberate upgrade guard.

### Note on the runtime names
The on-device install root, service and log directory remain
`deye-virtual-battery`, and the Python package remains `deye_virtual_battery`.
Renaming them would orphan the rollback backups of existing installs and break
in-place upgrades, for no user-visible benefit.

## 0.7.4

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
