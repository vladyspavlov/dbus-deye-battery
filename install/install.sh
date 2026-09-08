#!/bin/sh
# Install the Deye virtual battery adapter on a Venus OS GX device.
#
# Installing does NOT change how your system charges or discharges.  It only
# adds a battery service to D-Bus.  Two further, deliberate steps are needed
# before the adapter influences anything, and both are described in README.md:
#
#   1. selecting it under Settings -> System setup -> Battery monitor / BMS
#   2. handing CAN keepalive ownership over with service/handoff-can-owner
#
# Run from the unpacked source tree:  sh install/install.sh
set -eu

root=/data/deye-virtual-battery
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
stamp=$(date -u +%Y%m%dT%H%M%SZ)

[ -d /opt/victronenergy ] || {
    echo "This does not look like a Venus OS device (/opt/victronenergy missing)." >&2
    exit 1
}
velib=/opt/victronenergy/dbus-systemcalc-py/ext/velib_python
[ -d "$velib" ] || { echo "velib_python not found at $velib" >&2; exit 1; }

echo "installing from $source_dir into $root"

# Keep a timestamped copy of any existing install so a rollback is always
# possible.  These are never deleted automatically.
if [ -d "$root/src" ]; then
    backup=$root/.backup-src-$stamp
    echo "backing up existing source to $backup"
    cp -a "$root/src" "$backup"
fi

mkdir -p "$root/src" "$root/service/log" /data/log/deye-virtual-battery

cp -a "$source_dir/src/deye_virtual_battery" "$root/src/"
find "$root/src" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

for file in run handoff-can-owner rollback-can-owner; do
    cp "$source_dir/install/service/$file" "$root/service/$file"
    chmod 755 "$root/service/$file"
done
cp "$source_dir/install/service/log/run" "$root/service/log/run"
chmod 755 "$root/service/log/run"

# Kept beside the service so it is at a stable path however this was
# installed, and so it is still there after the source tree is tidied away.
cp "$source_dir/install/detect-can-interface.sh" "$root/detect-can-interface.sh"
chmod 755 "$root/detect-can-interface.sh"

if [ -f "$root/config" ]; then
    # 0.7.5 changed the default D-Bus service name from the model-specific
    # ...deye_se_f12 to the model-neutral ...deye_lv.  An install that never
    # pinned a name would silently move to the new one on upgrade, which
    # deselects it as battery monitor and BMS.  Pin the old name instead: an
    # upgrade must never change a running system's identity.
    if ! grep -q '^[[:space:]]*SERVICE_NAME=' "$root/config"; then
        echo "pinning the pre-0.7.5 D-Bus service name in $root/config"
        echo "SERVICE_NAME=com.victronenergy.battery.deye_se_f12" >> "$root/config"
    fi
else
    cp "$source_dir/install/config.example" "$root/config"
fi

# Persist across reboots and firmware updates.
if [ ! -f /data/rc.local ]; then
    cp "$source_dir/install/rc.local" /data/rc.local
    chmod 755 /data/rc.local
elif ! grep -q deye-virtual-battery /data/rc.local; then
    echo "/data/rc.local exists; append the contents of install/rc.local to it." >&2
    echo "The service will not start automatically until you do." >&2
fi

# Start it now.  /service is a tmpfs, so this link is re-made by rc.local.
if [ ! -e /service/deye-virtual-battery ]; then
    ln -s "$root/service" /service/deye-virtual-battery
fi
sleep 5
svc -u /service/deye-virtual-battery 2>/dev/null || true

echo
echo "installed. version: $(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$root/src/deye_virtual_battery/version.py")"
echo "config:    $root/config"
echo "logs:      tail -F /data/log/deye-virtual-battery/current"
echo "can port:  sh $root/detect-can-interface.sh"
echo "check:     dbus -y com.victronenergy.battery.deye_lv /Connected GetValue"
echo
echo "The adapter is running but NOT selected. It changes nothing until you"
echo "select it as the battery monitor / BMS in the GX menu."
