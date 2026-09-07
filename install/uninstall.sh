#!/bin/sh
# Remove the adapter and hand CAN back to the stock driver.
#
# Select "No BMS control" and restore your previous battery monitor in the GX
# menu BEFORE running this, so the system is never left without a source of
# charge limits.
set -eu

root=/data/deye-virtual-battery

if [ -f "$root/can-tx-enabled" ] && [ -x "$root/service/rollback-can-owner" ]; then
    echo "returning CAN keepalive ownership to the stock driver"
    "$root/service/rollback-can-owner"
fi

if [ -e /service/deye-virtual-battery ]; then
    svc -d /service/deye-virtual-battery 2>/dev/null || true
    sleep 2
    rm -f /service/deye-virtual-battery
fi

if [ -f /data/rc.local ] && grep -q deye-virtual-battery /data/rc.local; then
    echo "remove the deye-virtual-battery block from /data/rc.local by hand." >&2
fi

echo "service stopped and unlinked."
echo "Source, config and backups are left in $root; delete them yourself if"
echo "you are sure you will not roll back."
