#!/bin/sh
# One-command installer for the Deye LV battery driver on a Venus OS GX device.
#
#   wget -qO- https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/main/install/bootstrap.sh | sh
#
# It downloads a tagged release, checks that what arrived is what was asked
# for, and runs install/install.sh.  Everything is settable from the
# environment, because a piped script cannot take arguments:
#
#   VERSION=0.7.7        release to install; default is the latest one
#   CAN_INTERFACE=can1   skip auto-detection of the BMS-Can port
#   MODEL=SE-F12-C       shown in the GX device list
#   DEVICE_INSTANCE=513  preferred VRM instance; Venus may grant another
#   AUTO_DEVICE_INSTANCE=0  publish DEVICE_INSTANCE verbatim, write no setting
#   SERVICE_NAME=...     change only if you run more than one pack
#   SHA256=<hex>         refuse the download unless it matches
#   ALLOW_DOWNGRADE=1    permit installing older code than is running
#   DRY_RUN=1            show what would happen and stop
#
# Installing changes nothing about how your system charges or discharges.  The
# driver is not selected as battery monitor and does not transmit on CAN until
# you take two further deliberate steps, both described in README.md.
set -eu

REPO=vladyspavlov/dbus-deye-battery
BASE=/data
DEST_ROOT=/data/deye-virtual-battery

fail() { echo "$@" >&2; exit 1; }

require_venus() {
    [ -d /opt/victronenergy ] ||
        fail "This does not look like a Venus OS device (/opt/victronenergy missing)."
    [ "$(id -u)" = "0" ] || fail "Run this as root."
    [ -d /data ] || fail "/data is missing; this is not a supported GX layout."
}

# Venus ships both curl and BusyBox wget; either can do HTTPS to GitHub.
fetch() {
    url=$1
    out=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 --max-time 300 -o "$out" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$out" "$url"
    else
        fail "Neither curl nor wget is available."
    fi
}

latest_version() {
    tmp=$1
    fetch "https://api.github.com/repos/$REPO/releases/latest" "$tmp" ||
        fail "Could not reach the GitHub API. Set VERSION=x.y.z and retry."
    sed -n 's/.*"tag_name"[^"]*"v\{0,1\}\([^"]*\)".*/\1/p' "$tmp" | sed -n 1p
}

installed_version() {
    sed -n 's/^VERSION = "\(.*\)"$/\1/p' \
        "$DEST_ROOT/src/deye_virtual_battery/version.py" 2>/dev/null | sed -n 1p
}

# Numeric dotted compare; prints "older", "same" or "newer" for $1 against $2.
compare_versions() {
    left=$1
    right=$2
    index=1
    while [ "$index" -le 4 ]; do
        l=$(echo "$left" | cut -d. -f$index | sed 's/[^0-9].*//')
        r=$(echo "$right" | cut -d. -f$index | sed 's/[^0-9].*//')
        [ -n "$l" ] || l=0
        [ -n "$r" ] || r=0
        if [ "$l" -lt "$r" ]; then echo older; return; fi
        if [ "$l" -gt "$r" ]; then echo newer; return; fi
        index=$((index + 1))
    done
    echo same
}

main() {
    require_venus

    work=$BASE/.deye-bootstrap.$$
    mkdir -p "$work"
    # shellcheck disable=SC2064
    trap "rm -rf '$work'" EXIT HUP INT TERM

    version=${VERSION:-}
    if [ -z "$version" ]; then
        echo "resolving the latest release of $REPO"
        version=$(latest_version "$work/releases.json")
        [ -n "$version" ] || fail "Could not read a release tag. Set VERSION=x.y.z and retry."
    fi
    echo "target version: $version"

    running=$(installed_version || true)
    if [ -n "$running" ]; then
        echo "currently installed: $running"
        if [ "$(compare_versions "$version" "$running")" = older ] &&
            [ "${ALLOW_DOWNGRADE:-0}" != "1" ]; then
            fail "Refusing to install $version over the newer $running. Set ALLOW_DOWNGRADE=1 if that is really what you want."
        fi
    fi

    tarball=$work/source.tar.gz
    url="https://github.com/$REPO/archive/refs/tags/v$version.tar.gz"
    echo "downloading $url"
    fetch "$url" "$tarball" || fail "Download failed. Check the version exists on the Releases page."

    if [ -n "${SHA256:-}" ]; then
        actual=$(sha256sum "$tarball" | cut -d' ' -f1)
        [ "$actual" = "$SHA256" ] ||
            fail "Checksum mismatch: expected $SHA256, got $actual. Not installing."
        echo "checksum ok"
    fi

    tar xzf "$tarball" -C "$work" || fail "The download is not a readable archive."
    source_dir=$work/dbus-deye-battery-$version
    [ -f "$source_dir/install/install.sh" ] ||
        fail "The archive does not contain install/install.sh; refusing to continue."

    # The tag and the code must agree, or the version this driver reports to
    # VRM would not map back to the source you can read.
    packaged=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' \
        "$source_dir/src/deye_virtual_battery/version.py" | sed -n 1p)
    [ "$packaged" = "$version" ] ||
        fail "Tag v$version contains version $packaged. Refusing to install a mislabelled release."
    echo "verified: tag and packaged version both say $version"

    fresh=0
    [ -f "$DEST_ROOT/config" ] || fresh=1

    interface=${CAN_INTERFACE:-}
    if [ -z "$interface" ] && [ "$fresh" = 1 ]; then
        echo "detecting the BMS-Can interface"
        interface=$(sh "$source_dir/install/detect-can-interface.sh" --quiet 2>/dev/null || true)
        if [ -n "$interface" ]; then
            echo "detected: $interface"
        else
            echo "could not identify it; leaving the can0 default in place."
            echo "run install/detect-can-interface.sh afterwards to investigate."
        fi
    fi

    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo
        echo "DRY_RUN: would install $version from $source_dir into $DEST_ROOT"
        echo "DRY_RUN: CAN_INTERFACE=${interface:-can0} MODEL=${MODEL:-} DEVICE_INSTANCE=${DEVICE_INSTANCE:-513}"
        echo "DRY_RUN: AUTO_DEVICE_INSTANCE=${AUTO_DEVICE_INSTANCE:-1}"
        echo "DRY_RUN: nothing was written."
        return 0
    fi

    # Keep the unpacked release: uninstall.sh and the rollback notes in the
    # README are written against a source tree, and the old ones are left
    # alone so an earlier release is always still on disk.
    kept=$BASE/dbus-deye-battery-$version
    if [ ! -e "$kept" ]; then
        cp -a "$source_dir" "$kept"
    fi
    rm -f "$BASE/dbus-deye-battery"
    ln -s "$kept" "$BASE/dbus-deye-battery"

    sh "$kept/install/install.sh"

    # Settings are only seeded on a first install.  Rewriting them on an
    # upgrade could silently move a working system to a different CAN port or
    # a different D-Bus identity.
    if [ "$fresh" = 1 ]; then
        for pair in "CAN_INTERFACE=${interface:-}" "MODEL=${MODEL:-}" \
            "DEVICE_INSTANCE=${DEVICE_INSTANCE:-}" "SERVICE_NAME=${SERVICE_NAME:-}" \
            "AUTO_DEVICE_INSTANCE=${AUTO_DEVICE_INSTANCE:-}"; do
            key=${pair%%=*}
            value=${pair#*=}
            [ -n "$value" ] || continue
            sed -i "s|^#\{0,1\}[[:space:]]*$key=.*|$key=$value|" "$DEST_ROOT/config"
            echo "config: $key=$value"
        done
        svc -t /service/deye-virtual-battery 2>/dev/null || true
    else
        echo
        echo "Existing config left untouched: $DEST_ROOT/config"
    fi

    cat <<EOF

Next, and only when you are ready:

  1. watch it:    tail -F /data/log/deye-virtual-battery/current
  2. select it:   Settings -> System setup -> Battery monitor
  3. limits too:  Settings -> System setup -> Charge control -> Controlling BMS

Until step 2 the driver changes nothing. Source kept at $kept.
EOF
}

main "$@"
