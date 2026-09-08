#!/bin/sh
# Work out which CAN interface your battery is actually wired to.
#
# Read-only.  It reads link state, the Venus CAN-bus profile setting, the stock
# driver's service name, and listens passively for a couple of seconds.  It
# never brings an interface up or down, never changes a setting, and never
# transmits a frame.
#
#   sh install/detect-can-interface.sh          # explain every interface
#   sh install/detect-can-interface.sh --quiet  # print just the best guess
set -eu

quiet=0
[ "${1:-}" = "--quiet" ] && quiet=1

# Venus stores the GX menu's "CAN-bus profile" choice per interface.  Profile 3
# is "CAN-bus BMS LV (500 kbit/s)", which is the one a Deye pack needs; it is
# the value this driver was developed against.  0 is Disabled, and the rest are
# VE.Can, RV-C, Oceanvolt and CANopen variants that do not carry BMS frames.
BMS_PROFILE=3

say() { [ "$quiet" -eq 1 ] || echo "$@"; }

setting() {
    # A settings path answers with a bare value; a service path prefixes
    # "value = ".  An unknown path raises, so only a plain number is trusted.
    dbus -y com.victronenergy.settings "$1" GetValue 2>/dev/null |
        sed -n '1s/^value = //;1p' | grep -E '^[0-9]+$' || true
}

bitrate_of() {
    ip -details link show "$1" 2>/dev/null | tr -s ' ' |
        sed -n 's/.*bitrate \([0-9]*\).*/\1/p' | sed -n 1p
}

is_up() {
    ip link show "$1" 2>/dev/null | grep -q 'state UP'
}

# A bounded passive listen.  candump's own -n/-T normally end it; the loop is a
# hard backstop so this can never sit on a live system forever.  The only
# process it signals is the child it started itself.
sniff() {
    interface=$1
    out=$2
    candump -n 25 -T 2500 "$interface" > "$out" 2>/dev/null &
    child=$!
    waited=0
    while [ "$waited" -lt 4 ]; do
        kill -0 "$child" 2>/dev/null || break
        sleep 1
        waited=$((waited + 1))
    done
    kill "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
}

interfaces=$(ls /sys/class/net 2>/dev/null | grep '^can[0-9]*$' || true)
if [ -z "$interfaces" ]; then
    echo "No CAN interface exists on this device." >&2
    echo "Check Settings -> Services that BMS-Can is enabled." >&2
    exit 1
fi

# The stock Victron BMS driver is named after the interface it was told to use,
# e.g. can-bus-bms.can0.  If it is running, Venus has already decided which
# port is the BMS port, and that is the strongest hint available.
stock=$(ls /service 2>/dev/null | sed -n 's/^can-bus-bms\.\(can[0-9]*\)$/\1/p' | sed -n 1p || true)

best=""
best_rank=0

for interface in $interfaces; do
    profile=$(setting "/Settings/Canbus/$interface/Profile")
    bitrate=$(bitrate_of "$interface")
    state=down
    is_up "$interface" && state=up

    frames=""
    deye=no
    if [ "$state" = up ]; then
        tmp=/tmp/deye-detect-$interface.$$
        sniff "$interface" "$tmp"
        frames=$(wc -l < "$tmp" 2>/dev/null | tr -d ' ')
        # 351 (limits), 355 (SOC), 356 (measurements) and 35E (identity) are
        # sent by every Deye LV pack on both of its protocol profiles.
        if grep -qiE ' (351|355|356|35E)( +\[[0-9]\])? ' "$tmp" 2>/dev/null; then
            deye=yes
        fi
        rm -f "$tmp"
    fi

    # Ranked so that seeing the battery beats being configured for it, and
    # being configured for it beats merely running at the right speed.
    rank=0
    [ "$state" = up ] && rank=1
    [ "$bitrate" = "500000" ] && rank=2
    [ "${profile:-}" = "$BMS_PROFILE" ] && rank=3
    [ "$interface" = "${stock:-}" ] && rank=4
    [ "$deye" = yes ] && rank=5
    if [ "$rank" -gt "$best_rank" ]; then
        best=$interface
        best_rank=$rank
    fi

    say "$interface"
    say "  link          $state"
    say "  bitrate       ${bitrate:-unknown} (BMS-Can must be 500000)"
    say "  Venus profile ${profile:-unset} (want $BMS_PROFILE = CAN-bus BMS LV 500 kbit/s)"
    say "  stock driver  $([ "$interface" = "${stock:-}" ] && echo "can-bus-bms.$interface is running here" || echo "not on this interface")"
    say "  Deye frames   $deye (${frames:-0} frames seen while listening)"
    say ""
done

if [ -z "$best" ] || [ "$best_rank" -lt 2 ]; then
    say "No interface looks like a working BMS-Can port."
    say "In the GX menu set Settings -> Services -> <the BMS-Can port> ->"
    say "CAN-bus profile to 'CAN-bus BMS LV (500 kbit/s)', then run this again."
    exit 1
fi

if [ "$best_rank" -lt 5 ]; then
    say "Best guess only: no Deye frames were decoded, so check your wiring and"
    say "termination before trusting this."
fi

if [ "$quiet" -eq 1 ]; then
    echo "$best"
else
    echo "Use CAN_INTERFACE=$best"
fi
