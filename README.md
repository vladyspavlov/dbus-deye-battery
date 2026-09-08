# dbus-deye-battery

**Victron Venus OS battery driver for Deye SE-F LV packs over BMS-Can.**

Decodes the Deye PCS CAN protocol directly and publishes a native
`com.victronenergy.battery.*` D-Bus service, so a Deye low-voltage pack appears
in Venus OS as itself — with correct limits, correct alarms and a correct
current sign — instead of being misidentified as an LG RESU.

[![tests](https://github.com/vladyspavlov/dbus-deye-battery/actions/workflows/ci.yml/badge.svg)](https://github.com/vladyspavlov/dbus-deye-battery/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/vladyspavlov/dbus-deye-battery?sort=semver)](https://github.com/vladyspavlov/dbus-deye-battery/releases)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)

📖 **English** · [Українська](README.uk.md)

---

## Contents

- [The problem this solves](#the-problem-this-solves)
- [Read this before installing](#-read-this-before-installing)
- [DANGER — wire ONLY two pins](#-danger--wire-only-two-pins-and-not-straight-through)
- [What it does](#what-it-does)
- [Compatibility and tested scope](#compatibility-and-tested-scope)
- [Field notes from the reference installation](#field-notes-from-the-reference-installation)
- [Installation](#installation)
- [Validate against your own recording first](#validate-against-your-own-recording-first)
- [FAQ](#faq)
- [How it is put together](#how-it-is-put-together)
- [Development](#development)
- [Related projects](#related-projects)
- [License](#license)

---

## The problem this solves

**Out of the box, Venus OS identifies a Deye SE-F pack as an LG RESU.**

Venus ships a closed CAN battery driver that works out the manufacturer from
the frames on the wire. Current Deye firmware no longer sends the vendor marker
that driver looks for, so it falls through to LG's product ID (`0xB004`). Three
things then go wrong, and none of them announce themselves:

1. **LG-specific protection logic is applied to your battery.** Venus binds an
   LG circuit-breaker detector to anything claiming that product ID. It watches
   for a voltage pattern LG packs produce and, when it thinks it sees one, it
   **switches the inverter off** — taking AC output down. It is not reading a
   Deye alarm; it synthesises the condition itself.
2. **Alarms are wrong.** The Deye reports its faults in vendor condition tables
   the stock driver does not decode. Real protections can go unreported, and
   conditions that are not faults can surface as ones that are.
3. **The current sign can be inverted.** Deye and Victron disagree on which
   direction is positive, and the answer changes with the battery's selected
   protocol — so charge can be displayed and logged as discharge.

This driver decodes the Deye protocol directly and publishes a normal
`com.victronenergy.battery.*` D-Bus service with an honest identity, so no
vendor-specific logic binds to it, alarms come from the Deye's own condition
tables, and the current sign is resolved from the frames actually on the wire.

---

## ⚠️ Read this before installing

This software influences the charge and discharge limits of a live
inverter/charger. Getting it wrong can damage a battery, damage an inverter,
or drop AC output to your loads.

- It is **not** a Victron product and is not supported by Victron or Deye.
- It has been developed and run against **one** installation. See
  [Compatibility and tested scope](#compatibility-and-tested-scope) for exactly
  what that means.
- `PolicyConfig.blocked_charge_cvl_v` (55.2 V on a 16-series pack) is a
  **commissioning value that has not been validated on a bench or by either
  vendor.** It is the voltage published while the battery blocks charge. Review
  it against your own pack before selecting this driver as your BMS.
- Installing changes nothing on its own. Selecting it as your BMS does.

Provided under the Apache License 2.0, **without warranty of any kind**. You
are responsible for your own system.

---

## ☠️ DANGER — wire ONLY two pins, and NOT straight through

**Two conductors. Deye pin 4 → Victron pin 7, Deye pin 5 → Victron pin 8.**
The pin numbers differ on each side, so a straight-through cable is wrong.

### The only two connections that may exist

```
      DEYE  "PCS" port                     VICTRON  BMS-Can / VE.Can
      ┌───────────────────┐                ┌───────────────────┐
      │ pin 1   ○         │                │         ○   pin 1 │
      │ pin 2   ○         │                │         ○   pin 2 │
      │ pin 3   ○         │                │         ○   pin 3 │
      │ pin 4   ●  CANH ──┼─────────┐      │         ○   pin 4 │
      │ pin 5   ●  CANL ──┼──────┐  │      │         ○   pin 5 │
      │ pin 6   ○         │      │  │      │         ○   pin 6 │
      │ pin 7   ○         │      │  └──────┼──● CAN-H  pin 7   │
      │ pin 8   ○         │      └─────────┼──● CAN-L  pin 8   │
      └───────────────────┘                └───────────────────┘

        ● wired          ○ MUST be left completely unconnected

              Deye 4  ──────────────►  Victron 7      (CANH)
              Deye 5  ──────────────►  Victron 8      (CANL)

        The two wires run parallel — they do NOT cross each other.
        But pin 4 does NOT go to pin 4:  this is not a patch cable.
```

### Why a straight-through cable destroys the BMS

An ordinary Ethernet patch cable joins all eight conductors, pin 1 to pin 1
and so on. That does two damaging things at once:

- Victron's CAN-H and CAN-L (pins 7 and 8) land on **Deye pins 7 and 8, which
  are RS485** — CAN levels driven straight into the battery's RS485
  transceiver.
- The Victron side is not a passive data port. Pins other than 7 and 8 carry
  **supply and return voltages**, and a patch cable delivers those onto the
  Deye connector too.

Treat any extra conductor as a dead BMS. Not "may cause problems" — assume the
battery's BMS will be destroyed.

Victron's own guidance says exactly this:

> *"Only use CAN-H and CAN-L. No other wires."*

**Buy the correct cable, or crimp one with two wires and ring it out with a
meter before it goes anywhere near the battery.**

### Also

- Use the battery's **`PCS`** port. The `IN` and `OUT` ports are for
  battery-to-battery parallel links and have a different pinout again.
- Do not wire GND. Victron advises against it on non-isolated GX ports because
  it creates a ground loop.
- Terminate the bus per
  [Victron's cable guidance](https://www.victronenergy.com/live/battery_compatibility:can-bus_bms-cable).

---

## What it does

- Decodes the Deye PCS CAN protocol from a passively observed SocketCAN
  interface — limits, SOC/SOH, cell extrema, temperatures, MOS state, fault
  tables, pack history.
- **Detects which protocol the battery is speaking** and adapts. Deye packs
  expose a selectable inverter protocol; the `Sol-ark` and `victronCAN`
  settings differ in which frames are sent, in the `0x35E` identity, and in
  the **sign of the current in `0x356`**. Getting that sign wrong inverts
  charge and discharge. The driver resolves it from frames on the wire rather
  than from configuration.
- **Detects the pack rather than assuming a model.** Capacity, series cell
  count and every derived pack voltage threshold are measured from the battery.
- Publishes an honest identity (`ProductId 0xFFFF`), so no vendor-specific
  Venus logic binds to it, and the pack serial on the standard `/Serial` path.
  The serial arrives split across `0x600` and `0x650`; the driver joins the two
  halves and publishes nothing at all until both have decoded, because half a
  serial looks like a whole one.
- Maps Deye fault tables to real Venus alarms instead of suppressing them.
- Optionally owns the `0x305`/`0x307` inverter keepalive, but only after an
  explicit, verified handover.
- **Never** writes a Venus setting, a VE.Bus mode, or a DVCC parameter.

### What it deliberately does not do

- It does not invent a charge current the battery did not request. Deye packs
  routinely request CCL 0 A at 100 % SOC. Victron's own compatibility notes
  warn that a BMS which *"blocks charge, or discharge current, or sets CCL to 0
  when full, can trigger a number of confusing or misleading inverter/charger
  warnings and alarms."* This driver reports the shortfall as a diagnostic; it
  does not paper over it.
- It does not derive CVL from measured pack voltage minus an offset. That
  creates a feedback loop that can walk the target away from the intended
  value.
- Alarms never move a limit. They are telemetry, not control inputs.

---

## Compatibility and tested scope

**Nothing in the driver is tied to one battery model.** Capacity, series cell
count, protocol profile and current sign are all read from the battery, and
every pack voltage threshold scales from the measured series count. The only
model-specific value is the name shown in the GX device list, which no Deye
pack transmits — set it with `MODEL=` if you want your exact variant displayed.

| Model | Status |
|---|---|
| **Deye SE-F12-C** | Verified — developed and running against one installation |
| **Deye SE-F5-C** | Expected to work, unconfirmed on hardware |
| **Deye SE-F16-C** | Expected to work, unconfirmed on hardware |
| Other Deye LV packs on the PCS CAN protocol | Plausible, unconfirmed |

The SE-F series shares one PCS interface, so the other variants should speak
the same protocol — differing only in capacity and possibly series count, both
of which the driver measures rather than assumes.

Nobody has confirmed any of that on hardware. If you run it on anything other
than an SE-F12-C, please [open an issue](https://github.com/vladyspavlov/dbus-deye-battery/issues)
with a `candump` log — that is the single most useful thing you can contribute,
and it is what turns "expected to work" into "tested".

The reference installation:

| | |
|---|---|
| Battery | Deye SE-F12-C, 230 Ah, 16-series |
| Protocols | `Sol-ark` (Deye native) and `victronCAN`, both verified on the wire |
| Inverter | Victron MultiPlus-II GX 6k5 |
| Venus OS | v3.75, armv7l, Python 3.12 |
| Bus | BMS-Can, 500 kbit/s, classical 11-bit frames |

What the driver measures for itself:

| Property | Source |
|---|---|
| Nominal capacity | `0x35E` bytes 6-7 |
| Series cell count | pack voltage ÷ mean cell voltage, cross-checked |
| Pack voltage thresholds | series count × per-cell limits |
| Protocol profile | frames present on the wire |
| Current sign | follows the detected profile |
| Serial | `0x600` + `0x650` |
| Firmware marker | `0x500` / `0x363` / `0x35F` |

If the series count cannot be measured — cell voltages missing, or the numbers
disagree — the driver falls back to 16 and publishes `/HardwareVersion` as
`… (assumed)` rather than pretending it measured something. Override with
`CELL_COUNT=` if your pack is different and its cell data is unavailable.

---

## Field notes from the reference installation

### Update the BMS firmware before anything else

On its original firmware this pack intermittently tripped an **AFE
short-circuit-discharge protection (AFE-SCD)** with no real fault present. The
BMS opened its discharge path, the battery dropped off the bus, and the Victron
shut down as a consequence. Updating the BMS firmware stopped it.

If you are chasing unexplained dropouts on a Deye SE-F pack, do the firmware
update before suspecting your cabling, your GX, or a driver.

This driver decodes that condition rather than hiding it —
`afe_short_circuit_discharge` and its latched variant both publish as
`/Alarms/HighDischargeCurrent` level 2 — so if it ever does fire you can see it
in Venus instead of inferring it from an outage.

### How the firmware update is done

Over the air from the **Deye Cloud** app, over Bluetooth — not from the
inverter or the GX. The SE-F12 manual confirms the transport but not the
procedure:

> *"As your device is designed to possess Bluetooth function, it can connect to
> the Deye Cloud App via Bluetooth. Following successful login and
> registration, users can retrieve information about battery packs or the
> entire system."*
> — Deye SE-F12 user manual, issue 05

The manual stops there, so the rest is from doing it:

1. Open **Deye Cloud**, sign in.
2. **Three dots, top right → Local mode.**
3. Pick the device beginning with **`BAT…`** — that is the battery, not the
   inverter.
4. When it asks for a QR code, **the code is the battery's own serial
   number**, printed on the pack label. The manual documents no QR code for
   this step — its QR code points at the app operation manual instead — so if
   you cannot scan the label, entering the serial is what works.
5. Choose the OTA image and keep the app in the foreground until it finishes.

The battery restarts at the end. Expect roughly a 15-second gap in CAN frames
while it does, which the driver rides out as normal staleness.

> **Do not update while the battery is the only source of power.** The BMS
> restarts, and this driver's limits go stale during the gap. Do it on grid,
> with the inverter able to keep the loads up without the battery.

The update on the reference pack:

| | |
|---|---|
| Previous | `LVESS1525814N01` |
| Current | `LVESS1526701N01_F005` |

**You can identify the running firmware from CAN alone**, without the app.
`0x500` bytes 0-1 changed from `F0 02` to `F0 05` across the update, while
`identity.boot_version` stayed `V1.0F`. Rendered as hex digits those bytes read
`F002` and `F005` — and `F005` is exactly the suffix on the installed image
name above.

That correspondence is *probable, not confirmed*: it matches on the one pairing
that can be checked, since the older image name carries no `F` suffix to
compare against. Treat `0x500` bytes 0-1 as a firmware marker you can diff
against your own earlier captures, not as a decoder for the vendor's version
string. The driver exposes the raw value as
`identity.pack_software_version_raw` (`752` and `1520` respectively); replay a
capture through `tools/validate_real_captures.py` to read yours.

The change is sharply visible in a recording: on this system the app reported
the OTA in progress at 18:07 UTC and the new version word appeared on the CAN
bus at 18:13:15 UTC, with a roughly 15-second gap in battery frames where the
BMS restarted.

### The newer firmware adds CAN protocol selection

The battery gained a selectable inverter protocol. The default is `Sol-ark`;
`victronCAN` is the alternative. The reference installation runs `victronCAN`.

The two are not cosmetic variants. `victronCAN` drops `0x359`, `0x35C`,
`0x361`, `0x363`, `0x364` and `0x371`, adds Victron's `0x35A` and `0x35F`,
changes the `0x35E` identity to `PYLON`, and **inverts the sign of the current
in `0x356`**. A driver that assumes one profile will report charge as discharge
on the other.

This driver detects the active profile from the frames themselves, so both
settings work and switching between them needs no reconfiguration. Switching is
a live control change on a running system, so do it deliberately.

One caveat worth knowing before you switch: on this pack **every `0x35A` field
reports "not supported"**, so `victronCAN` does not actually give you working
Victron-standard alarms. The Deye `0x110` condition tables remain the real
alarm source, which is why this driver keeps decoding them in both profiles.

### Firmware alone was not the whole story

The AFE-SCD dropouts stopped after the update. A separate effect did not: with
the pack full and its charge MOS open, the battery is decoupled from the
inverter DC bus, and during large load steps VE.Bus briefly reports a DC
voltage several volts above the battery's own reading. Those excursions
continued after the firmware update.

They appear harmless in themselves — but a brief high reading is exactly what
vendor-specific Venus protection logic reacts to, and that logic binds on
battery identity. It is the reason this driver publishes a neutral
`ProductId 0xFFFF`.

---

## Installation

### Quick install

On the GX, as root:

```sh
wget -qO- https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/main/install/bootstrap.sh | sh
```

That resolves the latest release, checks that the code inside really is the
version its tag claims, works out which CAN port your battery is on, installs
into `/data` so it survives firmware updates, and starts the service. It takes
a few seconds and needs nothing else installed.

**It does not change how your system charges or discharges.** The driver ends
up running and publishing a battery service that nothing is consuming yet.
Two further deliberate steps — [selecting it](#select-it-as-your-battery-monitor)
and, only if you need it, [handing over CAN ownership](#can-keepalive-ownership-only-if-you-need-it)
— are what give it any influence.

A piped script cannot take arguments, so anything you want to set goes in the
environment in front of it:

```sh
CAN_INTERFACE=can1 MODEL=SE-F16-C \
  wget -qO- https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/main/install/bootstrap.sh | sh
```

| Variable | Default | What it does |
| --- | --- | --- |
| `VERSION` | latest release | Install an exact release instead |
| `CAN_INTERFACE` | auto-detected | Skip detection and use this port |
| `MODEL` | none | Variant shown in the GX device list, e.g. `SE-F12-C` |
| `DEVICE_INSTANCE` | `513`, only a preference | The instance to ask Venus for; it grants that one when free and the next free one otherwise — [details](#device-instance-and-running-more-than-one-pack) |
| `AUTO_DEVICE_INSTANCE` | `1` on a fresh install, `0` on upgrade | `0` skips the negotiation, publishes `DEVICE_INSTANCE` verbatim and writes no Venus setting |
| `SERVICE_NAME` | `com.victronenergy.battery.deye_lv` | Change only if you run more than one pack |
| `SHA256` | none | Refuse the download unless it matches this checksum |
| `ALLOW_DOWNGRADE` | unset | Permit installing older code than is running |
| `DRY_RUN` | unset | Print what would happen and write nothing |

Settings are only seeded on a **first** install. On an upgrade your existing
`/data/deye-virtual-battery/config` is left as it is, with two deliberate
exceptions: if it names no `SERVICE_NAME`, or no `AUTO_DEVICE_INSTANCE`,
`install.sh` appends whichever value the system is already running with. Both
of those are identity, and silently moving a working system onto a different
CAN port, a different D-Bus name or a different VRM instance is a good way to
lose your battery monitor.

### Reading it before you run it

Piping a script from the internet into a root shell is a reasonable thing to be
uneasy about, on a machine that controls an inverter especially. All three of
these are supported:

```sh
# 1. Read it first, then run the copy you read.
wget -qO bootstrap.sh https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/main/install/bootstrap.sh
less bootstrap.sh
DRY_RUN=1 sh bootstrap.sh       # says what it would do, writes nothing
sh bootstrap.sh

# 2. Pin the release archive to a checksum you obtained yourself.
VERSION=0.7.7 SHA256=<sha256 of the tarball> sh bootstrap.sh

# 3. Skip the bootstrap entirely and do it by hand -- see below.
```

The script is deliberately built so that a **truncated** download does nothing
at all: every action lives in a function, and the only line that runs anything
is the last one. A copy that arrives half-way defines some functions and exits.

### Prerequisites

- Root SSH access to your GX device
  ([Victron's guide](https://www.victronenergy.com/live/ccgx:root_access#how_to_obtain_root_access)).
- The battery wired to **BMS-Can** with a
  [correctly terminated cable](https://www.victronenergy.com/live/battery_compatibility:can-bus_bms-cable),
  and that port set to **CAN-bus BMS LV (500 kbit/s)** under
  `Settings → Services`.
- Frames actually arriving. The next section is how to prove that.

### Finding your BMS-Can interface

`can0` is right on most GX devices, but not on all of them. A Cerbo GX has both
VE.Can and BMS-Can, and which kernel interface each becomes depends on the
model and the firmware; on a GX with an added USB-CAN adapter it can be `can2`
or higher. Installing against the wrong port gives you a service that starts,
never qualifies, and never comes online — with nothing obviously wrong.

The quick install works this out for you. To see its reasoning, or to check an
existing install, run it directly:

```sh
sh /data/deye-virtual-battery/detect-can-interface.sh
```

```text
can0
  link          up
  bitrate       500000 (BMS-Can must be 500000)
  Venus profile 3 (want 3 = CAN-bus BMS LV 500 kbit/s)
  stock driver  can-bus-bms.can0 is running here
  Deye frames   yes (25 frames seen while listening)

Use CAN_INTERFACE=can0
```

It is read-only: it reads settings and listens, and never brings an interface
up or down, changes a setting, or transmits a frame.

It combines four independent signals, weakest first. Each is worth knowing on
its own, because if detection fails these are what you check by hand:

1. **What CAN ports exist at all.**

   ```sh
   ls /sys/class/net | grep '^can'
   ```

2. **Link state and bitrate.** BMS-Can must be up at 500 kbit/s. A port at
   250000 is configured as VE.Can, not BMS-Can.

   ```sh
   ip -details link show can0 | grep -E 'state|bitrate'
   ```

3. **The Venus CAN-bus profile.** This is the setting behind
   `Settings → Services → …`, and it is the authoritative answer to *what did
   the operator configure this port as*:

   ```sh
   dbus -y com.victronenergy.settings /Settings/Canbus/can0/Profile GetValue
   ```

   `3` is **CAN-bus BMS LV (500 kbit/s)** — the one a Deye pack needs. `0` is
   disabled; the other values are VE.Can, VE.Can + CAN-bus BMS, CAN-bus BMS HV,
   Oceanvolt, RV-C and CANopen variants, none of which carry the frames this
   driver decodes.

4. **What Venus itself decided.** The stock driver's service is named after the
   port it was started on, so this is Venus telling you which one it considers
   the BMS port:

   ```sh
   ls /service | grep can-bus-bms      # e.g. can-bus-bms.can0
   ```

5. **The frames on the wire**, which beats all of the above:

   ```sh
   candump -n 20 can0
   ```

   You want `351`, `355`, `356` and `35E`. If the port is up and correctly
   configured but silent, the problem is the cable — go back to
   [the wiring warning](#-danger--wire-only-two-pins-and-not-straight-through),
   because a straight-through RJ45 patch lead is the usual cause and it can
   damage the BMS.

Then pass what you found to the installer, or put it in the config afterwards:

```sh
CAN_INTERFACE=can1 wget -qO- https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/main/install/bootstrap.sh | sh
```

### Installing by hand instead

The bootstrap only automates this; there is nothing it does that you cannot do
yourself. Install a **release**, not the `main` branch — a release is a fixed
set of files, so the version the driver reports on D-Bus and in VRM always maps
back to exact code, which is what you need when something misbehaves at 2am.

```sh
VERSION=0.7.7          # see the Releases page for the current one

cd /data
wget -O deye.tar.gz https://github.com/vladyspavlov/dbus-deye-battery/archive/refs/tags/v$VERSION.tar.gz
tar xzf deye.tar.gz
cd dbus-deye-battery-$VERSION
sh install/install.sh
```

Installing from `main` is fine for development, but `main` moves. Two people
installing a week apart get different code with no way to tell, which is a poor
property for something that writes charge limits to an inverter.

The installer backs up any existing install to a timestamped
`/data/deye-virtual-battery/.backup-src-*` directory, writes the source to
`/data` so it survives firmware updates, registers a `/data/rc.local` hook,
and starts the service.

**Nothing about your system's behaviour has changed yet.** The driver is now
publishing a battery service that nothing is consuming.

### Upgrading

Exactly the same command as the quick install. It backs the current source up
first, leaves your config alone, and refuses to go backwards unless you say
`ALLOW_DOWNGRADE=1`. To roll back, point the source directory at one of the
`.backup-src-*` trees the installer kept and restart the service.

### Check it is healthy

```sh
tail -F /data/log/deye-virtual-battery/current

dbus -y com.victronenergy.battery.deye_lv /Connected              GetValue
dbus -y com.victronenergy.battery.deye_lv /Dc/0/Voltage           GetValue
dbus -y com.victronenergy.battery.deye_lv /Soc                    GetValue
dbus -y com.victronenergy.battery.deye_lv /Info/MaxChargeVoltage  GetValue
dbus -y com.victronenergy.battery.deye_lv /Diagnostics/Lifecycle/State GetValue
```

`/Connected` should be `1` and lifecycle state `'online'`. It should also
appear in the GX menu under **Settings → Device list**, named `Deye LV battery`
unless you set `MODEL=`.

Confirm the current sign matches reality before going further: discharging
must show **negative** `/Dc/0/Current`. Check the detected profile with

```sh
dbus -y com.victronenergy.battery.deye_lv /Diagnostics/Profile/BmsProtocol GetValue
```

### Configure (optional)

Edit `/data/deye-virtual-battery/config` — see
[`install/config.example`](install/config.example) for every option. The two
most common:

```sh
CAN_INTERFACE=can1       # Cerbo GX often has BMS-Can on can1
MODEL=SE-F12-C           # your exact variant, shown in the GX device list
```

If you run more than one pack, see
[Device instance](#device-instance-and-running-more-than-one-pack) below.

Then restart: `svc -t /service/deye-virtual-battery`

### Device instance, and running more than one pack

Skip this unless you have two packs or a second CAN BMS. The default works.

Every `com.victronenergy.battery` on a GX needs its own **VRM device
instance**. It is what **Settings → System setup → Battery monitor** points
at, and what VRM keys this device's history on. The default here is `513`, and
that is not an arbitrary number: the stock `can-bus-bms` driver takes **512**
on `can0`, so 513 sits one above it.

That offset is only correct for that one arrangement. On a GX where BMS-Can is
`can1` — the normal wiring on a Cerbo GX, and what this README recommends — the
stock driver may take 513 itself. Two packs both running this driver collide
outright.

So by default a fresh install lets Venus resolve it, the way Victron documents
and the way the stock driver itself behaves:

```sh
AUTO_DEVICE_INSTANCE=1     # in /data/deye-virtual-battery/config
```

The driver asks localsettings to reserve an instance under
`/Settings/Devices/<id>/ClassAndVrmInstance`, keyed on the pack serial read
from CAN (falling back to the interface name). localsettings grants
`DEVICE_INSTANCE` when it is free and the next free number otherwise, then
remembers the mapping. **Two packs therefore come up as 513 and 514 with no
configuration at all**, and stay there across reboots and firmware updates.

This is the only setting the driver ever writes, it lives under
`/Settings/Devices`, and it is an identity mapping — never a charge, discharge,
DVCC or VE.Bus setting. Two guards make it safe to leave on:

- **An instance the system is currently selecting is never moved.** If
  `/Settings/SystemSetup/BatteryService` already points at the configured
  number, the driver publishes that number and does not ask localsettings
  anything.
- **An existing reservation is reused, never replaced.**

Set `AUTO_DEVICE_INSTANCE=0` if you want a guarantee that the driver writes no
setting whatsoever; the configured `DEVICE_INSTANCE` is then published
verbatim. `install.sh` sets `0` automatically when it upgrades an install made
before this option existed, so an upgrade can never move a running system's
instance.

Check what happened:

```sh
dbus -y com.victronenergy.battery.deye_lv /DeviceInstance GetValue
dbus -y com.victronenergy.battery.deye_lv /Diagnostics/Instance/Source GetValue
```

`Source` reads `configured`, `pinned-to-selection`, `reserved`, `allocated` or
`fallback`.

### Select it as your battery monitor

This is the step that changes system behaviour.

**Settings → System setup → Battery monitor →** the Deye entry

To let it supply charge limits to DVCC as well:

**Settings → System setup → Charge control → Controlling BMS →** the Deye entry

Watch VE.Bus Battery Operational Limits update to the driver's values, and
watch your inverter for a few minutes before leaving it unattended.

To back out: set **Controlling BMS → No BMS control** and restore your
previous battery monitor. That is instant and needs no uninstall.

### CAN keepalive ownership (only if you need it)

Skip this unless you are removing the stock driver. It stops
`can-bus-bms.can0` and takes over transmitting `0x305`/`0x307`.

You need this if the stock driver is still publishing a misidentified battery
service, because vendor-specific Venus logic binds to *any* battery service
with a matching product ID — not just the selected one.

```sh
/data/deye-virtual-battery/service/handoff-can-owner
```

It verifies preconditions first (service online, CAN fresh, no critical
staleness, discharge permitted, AC output live) and refuses if any fail.
Reverse it at any time:

```sh
/data/deye-virtual-battery/service/rollback-can-owner
```

### Uninstalling

Restore your previous battery monitor and set **No BMS control** in the GX
menu **first**, then run `install/uninstall.sh` from the unpacked source tree:

```sh
sh /data/dbus-deye-battery-$VERSION/install/uninstall.sh
```

Source and backups are left in `/data/deye-virtual-battery` so you can roll
back.

### A note on the two names

The GitHub project is `dbus-deye-battery`, following the Victron convention for
Venus OS D-Bus drivers. On the device, the install root, the service and the
log directory are `deye-virtual-battery`, and the Python package is
`deye_virtual_battery`. Those runtime names are deliberately **not** renamed:
existing installations run under them, and renaming would orphan their rollback
backups and break in-place upgrades.

---

## Validate against your own recording first

You do not need a GX to test the decoder, and you should not let it near a
live inverter until you have. Record a few minutes on your GX:

```sh
candump -L -x can0 > my-battery.log
```

Copy it to a PC and replay it:

```sh
pip install -e ".[dev]"
python -m deye_virtual_battery replay my-battery.log
python tools/validate_real_captures.py my-battery.log
```

The validator reports decode errors, unknown frames, missing required frames
for the detected profile, the effective limits, and — importantly — whether
the policy layer would ever have stopped discharge or switched the inverter
off. Two redacted sample recordings are in `tests/data/` if you want to see
the expected output.

**The pack serial is masked in all offline output**, including nested models,
because this is exactly the output people paste into bug reports. It is still
published normally on the device's own D-Bus, where Venus and VRM want it. Pass
`--show-serial` when you genuinely need it locally.

Raw `candump` logs are a different matter: they carry the serial in `0x600` and
`0x650` in the clear. Redact those two frames before sharing a capture — the
bundled samples show the shape.

---

## FAQ

**Why does Venus OS show my Deye battery as an LG RESU?**
Because the stock Venus CAN driver infers the manufacturer from the frames, and
current Deye firmware no longer sends the marker it looks for. It falls through
to LG's product ID. See [The problem this solves](#the-problem-this-solves).

**Can Venus OS really switch my inverter off because of this?**
Yes. Venus binds LG circuit-breaker detection to any battery service with LG's
product ID, and that logic can write a VE.Bus mode that drops AC output. It is
synthesised by Venus, not reported by the Deye.

**My battery's charge and discharge are the wrong way round. Why?**
The current sign in `0x356` depends on the battery's selected inverter
protocol. `Sol-ark` and `victronCAN` disagree. This driver detects which one is
active and applies the right sign.

**Which CAN interface is my battery on — can0 or can1?**
Run `sh /data/deye-virtual-battery/detect-can-interface.sh`, or check
`dbus -y com.victronenergy.settings /Settings/Canbus/can0/Profile GetValue`
yourself: `3` is CAN-bus BMS LV at 500 kbit/s. See
[Finding your BMS-Can interface](#finding-your-bms-can-interface).

**Is `curl | sh` safe here?**
You do not have to use it. The bootstrap supports `DRY_RUN=1`, takes a `SHA256`
to pin the release archive, verifies that the tag and the packaged version
agree before installing anything, and is written so a truncated download does
nothing. Or install by hand — it is six commands. See
[Reading it before you run it](#reading-it-before-you-run-it).

**Do I have to remove the stock `can-bus-bms` driver?**
Not to try it — installing changes nothing until you select it. You do need to
hand over CAN ownership if the stock driver keeps publishing a misidentified
service, because Venus's vendor-specific logic binds to *any* matching service,
not just the selected one. See [CAN keepalive ownership](#can-keepalive-ownership-only-if-you-need-it).

**Will it work on an SE-F5-C or SE-F16-C?**
Probably — nothing in the driver is model-specific — but nobody has confirmed
it on hardware. See [Compatibility](#compatibility-and-tested-scope).

**Does it survive a Venus OS firmware update?**
The source lives in `/data` and is restored by a `/data/rc.local` hook, which
is Victron's own documented pattern for this.

**Can I run two Deye packs on one GX?**
Yes. Give each its own `SERVICE_NAME`, and leave `AUTO_DEVICE_INSTANCE=1` so
Venus hands out the device instances — they come up as 513 and 514 without you
choosing anything. See
[Device instance](#device-instance-and-running-more-than-one-pack).

**Does it write anything to my system?**
No VE.Bus mode, no DVCC parameter, no charge or discharge setting — ever. One
exception, and it is an identity mapping rather than a control: with
`AUTO_DEVICE_INSTANCE=1` it reserves its VRM device instance under
`/Settings/Devices/<id>/ClassAndVrmInstance`, which is the mechanism Victron
documents for exactly this and what the stock `can-bus-bms` driver does. Set
`AUTO_DEVICE_INSTANCE=0` and it writes nothing at all. Either way,
`/Diagnostics/Commissioning/NoSettingsWrites` reports what the running process
actually did. The only thing it transmits on CAN is the `0x305`/`0x307`
keepalive, and only after you explicitly hand ownership over.

---

## How it is put together

```
candump / decoder    parse frames into named fields with units and validity
profile              classify Sol-ark vs victronCAN, resolve the 0x356 sign
cache                per-field freshness, so a stale field is never used
policy               limits, alarms, permissions from a snapshot
stateful             debounce, hysteresis, lifecycle
dbus_model           shape it as D-Bus paths (no D-Bus dependency)
venus_instance       decide the VRM device instance (pure; no D-Bus)
venus_bms_publisher  the only layer that touches D-Bus or a CAN socket
```

Everything below `venus_bms_publisher` operates on plain data, which is why
the whole pipeline can be replayed and tested on a laptop.

The layout follows
[Victron's own driver guidance](https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-venus):
code in `/data` so it survives updates, a daemontools service symlinked from
`/service`, restoration via `/data/rc.local`, and fail-fast on error rather
than defensive recovery.

## Development

```sh
pip install -e ".[dev]"
pytest -q
```

No runtime dependencies. Tests run on any Linux/macOS machine — none of them
need a GX, a CAN interface, or D-Bus. Three extended tests are skipped unless
you drop your own recordings into `tests/data/private/`.

Before pushing, run what CI runs:

```sh
sh tools/preflight.sh            # shell syntax + the full suite
sh tools/preflight.sh --matrix   # also the oldest and newest supported
                                 # Python, in Docker
```

The installer tests need `bubblewrap` and `dash`; without them they skip. The
`--matrix` run is the one worth the extra minute — every CI failure this
project has had was a difference between a developer's machine and the runner,
not a broken test.

### Releasing

`VERSION` in `src/deye_virtual_battery/version.py` is the only place the
version is authored. `pyproject.toml` and the newest `CHANGELOG.md` heading
must agree with it — CI fails if they do not — and the tag is cut from the
code rather than from an argument, so it cannot name something other than what
it ships:

```sh
sh tools/release.sh --dry-run   # every check, nothing pushed
sh tools/release.sh             # tag and push
```

Pushing the tag is the entire release. The workflow re-checks the version,
runs the suite against the tagged tree, and publishes that version's changelog
section as the release notes. A version that is never tagged reaches nobody,
so `sh tools/preflight.sh --version` says so out loud.

**Release when the installed behaviour changes, not on every commit.** The
version is published on `/Mgmt/ProcessVersion`, so it is what Venus, VRM and
every bug report call the running adapter — two numbers for identical code
make a report ambiguous rather than precise. Documentation, tests, CI and dev
tooling reach people from `main` directly and need no release; `src/` and
`install/` reach them only through the tarball, so the same check reports when
those have moved past the tag. The exception is a documentation fix that
corrects something dangerous — a wrong limit, a command that could damage
hardware — which is worth carrying into the artifact too.

[`docs/protocol-notes.md`](docs/protocol-notes.md) records what has been
confirmed against the vendor's own app and protocol document, what is only
probable, and what is still unknown.
[`CHANGELOG.md`](CHANGELOG.md) records what changed in each release.

Contributions welcome, especially:

- captures from other Deye models or firmware,
- the `0x400` system-status enumeration, which is still unnamed,
- the `0x110` bit for the app's fifth "Current Limit MOS" — it needs a capture
  taken while the pack is actively current-limiting,
- confirmation of the `0x35E` cell-manufacturer code `0x1C`.

## Related projects

- [dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)
  — the general-purpose Venus driver for serial, Bluetooth and CAN BMSes. If
  your battery is not a Deye, start there.
- [SetupHelper](https://github.com/kwindrem/SetupHelper) — package manager for
  Venus OS add-ons.
- [esphome-deye-bms](https://github.com/Psynosaur/esphome-deye-bms) — ESPHome
  decoder for the same protocol family.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for trademarks and
third-party attribution.
