# deye-virtual-battery

A Venus OS battery driver for **Deye SE-F12** low-voltage packs on BMS-Can.

Venus OS ships a closed CAN battery driver that identifies a battery from the
frames it sends. Current Deye firmware no longer sends the vendor marker that
driver looks for, so the pack is misidentified as a different manufacturer's
battery — and vendor-specific protection logic written for that other battery
is then applied to yours.

This driver decodes the Deye protocol directly and publishes a normal
`com.victronenergy.battery.*` D-Bus service, so Venus sees the pack as what it
actually is.

---

## ⚠️ Read this before installing

This software influences the charge and discharge limits of a live
inverter/charger. Getting it wrong can damage a battery, damage an inverter,
or drop AC output to your loads.

- It is **not** a Victron product and is not supported by Victron or Deye.
- It has been developed and run against **one** installation. See
  [Tested scope](#tested-scope) for exactly what that means.
- `PolicyConfig.blocked_charge_cvl_v` (55.2 V) is a **commissioning value that
  has not been validated on a bench or by either vendor.** It is the voltage
  published while the battery blocks charge. Review it against your own pack
  before selecting this driver as your BMS.
- Installing changes nothing on its own. Selecting it as your BMS does.

Provided under the Apache License 2.0, **without warranty of any kind**. You
are responsible for your own system.

---

## ☠️ DANGER — the CAN cable is not a straight-through Ethernet cable

**Using an ordinary patch cable between the Deye PCS port and Victron BMS-Can
will put Victron's CAN signals onto the battery's RS485 pins and can destroy
the battery BMS.** Only pins 4 and 5 on the Deye side may be connected.

Confirmed from both vendors' own documentation:

| | pin 1 | pin 2 | pin 3 | **pin 4** | **pin 5** | pin 6 | pin 7 | pin 8 |
|---|---|---|---|---|---|---|---|---|
| **Deye PCS port** | 485-B | 485-A | – | **CANH** | **CANL** | – | 485-A | 485-B |
| **Victron BMS-Can** | – | – | GND | – | – | – | **CAN-H** | **CAN-L** |

Only two conductors, crossed 4→7 and 5→8:

```
      DEYE  "PCS" port                      VICTRON  BMS-Can
      (RJ45)                                (RJ45)

   1  485-B   ○      ✗ leave unconnected       ○  1
   2  485-A   ○      ✗ leave unconnected       ○  2
   3   --     ○      ✗ leave unconnected       ○  3  GND
   4  CANH    ●──────────────┐                 ○  4
   5  CANL    ●───────────┐  │                 ○  5
   6   --     ○      ✗    │  │                 ○  6
   7  485-A   ○      ✗    │  └───────────────► ●  7  CAN-H
   8  485-B   ○      ✗    └──────────────────► ●  8  CAN-L

        ● = connected        ○ = MUST stay unconnected
```

**Why a straight-through cable is destructive:** Victron drives CAN-H on pin 7
and CAN-L on pin 8. Straight through, those land on Deye pins 7 and 8 — which
are **485-A and 485-B**. That feeds CAN transceiver levels straight into the
battery's RS485 transceiver.

Use the battery's `PCS` port, not `IN` or `OUT`. The `IN`/`OUT` ports are for
battery-to-battery parallel links and have a completely different pinout
(CANL/CANH on pins 1/2 and 7/8).

Terminate the bus per
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

## Tested scope

| | |
|---|---|
| Battery | Deye SE-F12-C, 230 Ah, 16-series |
| Protocols | `Sol-ark` (Deye native) and `victronCAN`, both verified on the wire |
| Inverter | MultiPlus-II GX 6k5 |
| Venus OS | v3.75, armv7l, Python 3.12 |
| Bus | BMS-Can, 500 kbit/s, classical 11-bit frames |

**Not tested, but likely compatible:** the **SE-F5-C** and **SE-F16-C** are
the same SE-F series with the same PCS interface, so they almost certainly
speak the same protocol — only the cell count and capacity should differ. The
driver reads capacity from the battery rather than assuming it, so it should
adapt. Nobody has confirmed this on hardware.

Other Deye families are unverified. If you run it on anything other than an
SE-F12-C, please open an issue with a `candump` log — that is the single most
useful thing you can contribute.

---

## Field notes from the reference installation

### Update the BMS firmware before anything else

On its original firmware this pack intermittently tripped an **AFE
short-circuit-discharge protection (AFE-SCD)** with no real fault present. The
BMS opened its discharge path, the battery dropped off the bus, and the Victron
shut down as a consequence. Updating the BMS firmware stopped it.

If you are chasing unexplained dropouts on an SE-F12, do the firmware update
before suspecting your cabling, your GX, or a driver.

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

### 0. Prerequisites

- Root SSH access to your GX device
  ([Victron's guide](https://www.victronenergy.com/live/ccgx:root_access)).
- The battery wired to **BMS-Can** with a
  [correctly terminated cable](https://www.victronenergy.com/live/battery_compatibility:can-bus_bms-cable),
  and that port set to **500 kbit/s** (`Settings → Services → BMS-Can`).
- Confirm frames are arriving before installing anything:

  ```sh
  candump -L -x can0 | head -20
  ```

  You should see `351`, `355`, `356`, `35E` at minimum. If the interface is
  `can1` on your GX, use that everywhere below.

### 1. Copy and install

```sh
cd /data
wget -O deye.tar.gz https://github.com/<you>/deye-virtual-battery/archive/refs/heads/main.tar.gz
tar xzf deye.tar.gz
cd deye-virtual-battery-main
sh install/install.sh
```

The installer backs up any existing install to a timestamped
`/data/deye-virtual-battery/.backup-src-*` directory, writes the source to
`/data` so it survives firmware updates, registers a `/data/rc.local` hook,
and starts the service.

**Nothing about your system's behaviour has changed yet.** The driver is now
publishing a battery service that nothing is consuming.

### 2. Check it is healthy

```sh
tail -F /data/log/deye-virtual-battery/current

dbus -y com.victronenergy.battery.deye_se_f12 /Connected              GetValue
dbus -y com.victronenergy.battery.deye_se_f12 /Dc/0/Voltage           GetValue
dbus -y com.victronenergy.battery.deye_se_f12 /Soc                    GetValue
dbus -y com.victronenergy.battery.deye_se_f12 /Info/MaxChargeVoltage  GetValue
dbus -y com.victronenergy.battery.deye_se_f12 /Diagnostics/Lifecycle/State GetValue
```

`/Connected` should be `1` and lifecycle state `'online'`. It should also
appear in the GX menu under **Settings → Device list** as `Deye SE-F12-C`.

Confirm the current sign matches reality before going further: discharging
must show **negative** `/Dc/0/Current`. Check the detected profile with

```sh
dbus -y com.victronenergy.battery.deye_se_f12 /Diagnostics/Profile/BmsProtocol GetValue
```

### 3. Configure (optional)

Edit `/data/deye-virtual-battery/config` — see `install/config.example` for
every option. The common one is a different CAN interface:

```sh
CAN_INTERFACE=can1
```

Then restart: `svc -t /service/deye-virtual-battery`

### 4. Select it as your battery monitor

This is the step that changes system behaviour.

**Settings → System setup → Battery monitor →** `Deye SE-F12-C`

To let it supply charge limits to DVCC as well:

**Settings → System setup → Charge control → Controlling BMS →** `Deye SE-F12-C`

Watch VE.Bus Battery Operational Limits update to the driver's values, and
watch your inverter for a few minutes before leaving it unattended.

To back out: set **Controlling BMS → No BMS control** and restore your
previous battery monitor. That is instant and needs no uninstall.

### 5. CAN keepalive ownership (only if you need it)

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
menu **first**, then:

```sh
sh /data/deye-virtual-battery/../deye-virtual-battery-main/install/uninstall.sh
```

Source and backups are left in `/data/deye-virtual-battery` so you can roll
back.

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

## How it is put together

```
candump / decoder    parse frames into named fields with units and validity
profile              classify Sol-ark vs victronCAN, resolve the 0x356 sign
cache                per-field freshness, so a stale field is never used
policy               limits, alarms, permissions from a snapshot
stateful             debounce, hysteresis, lifecycle
dbus_model           shape it as D-Bus paths (no D-Bus dependency)
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

[`docs/protocol-notes.md`](docs/protocol-notes.md) records what has been
confirmed against the vendor's own app and protocol document, what is only
probable, and what is still unknown.

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
