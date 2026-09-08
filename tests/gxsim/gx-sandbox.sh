#!/bin/sh
# Build a throwaway fake Venus OS GX and run a command inside it.
#
#   tests/gxsim/gx-sandbox.sh <workdir> <repo-root> <version> <command>...
#
# The point is to exercise install/bootstrap.sh and install/install.sh exactly
# as written -- absolute /data, /service and /opt/victronenergy paths included
# -- without going anywhere near a real inverter.  Nothing here talks to the
# network, to D-Bus or to a CAN interface: those are stubs that answer with
# values captured from a real GX running Venus OS v3.75.
#
# Run the scripts under dash, not "busybox sh".  Debian's BusyBox is built with
# a standalone shell, so it resolves svc, wget and ip to its own applets and
# ignores the stubs on PATH; a GX's BusyBox is not, and resolves them from PATH
# like dash does.  Dialect coverage comes from a separate "busybox sh -n" check.
#
# Requires bubblewrap.  Exits 127 if it is missing, so callers can skip.
set -eu

work=${1:?usage: gx-sandbox.sh <workdir> <repo-root> <version> <command>...}
repo=${2:?repo root}
version=${3:?version}
shift 3

command -v bwrap >/dev/null 2>&1 || exit 127

rm -rf "$work"
mkdir -p "$work/data" "$work/service" "$work/sysnet/can0" "$work/bin" \
    "$work/opt/dbus-systemcalc-py/ext/velib_python" "$work/fixtures"

# The release tarball the download stub will hand back, packed the way GitHub
# packs one: a single top-level <repo>-<version>/ directory owned by root, and
# only the files git would ship.  Getting the ownership right matters -- tar
# run as root restores it, and a tarball owned by a normal user fails to
# extract on a real GX.
staging=$work/staging/dbus-deye-battery-$version
mkdir -p "$staging"
(cd "$repo" && git ls-files --cached --others --exclude-standard) > "$work/filelist"
while IFS= read -r item; do
    [ -f "$repo/$item" ] || continue
    mkdir -p "$staging/$(dirname "$item")"
    cp -p "$repo/$item" "$staging/$item"
done < "$work/filelist"
tar czf "$work/fixtures/v$version.tar.gz" --owner=0 --group=0 \
    -C "$work/staging" "dbus-deye-battery-$version"

# Extra fixtures for upgrade, downgrade and mislabelled-tag cases.  Each is the
# same tree under a different tag; GX_MISLABELLED names the one whose version.py
# is deliberately left disagreeing with its tag.
for extra in ${GX_EXTRA_VERSIONS:-}; do
    other=$work/staging-$extra/dbus-deye-battery-$extra
    mkdir -p "$(dirname "$other")"
    cp -a "$staging" "$other"
    if [ "$extra" != "${GX_MISLABELLED:-}" ]; then
        sed -i "s/^VERSION = \".*\"$/VERSION = \"$extra\"/" \
            "$other/src/deye_virtual_battery/version.py"
    fi
    tar czf "$work/fixtures/v$extra.tar.gz" --owner=0 --group=0 \
        -C "$work/staging-$extra" "dbus-deye-battery-$extra"
done

# A non-executable file to shadow the real curl with, so the BusyBox wget
# branch of the installer gets exercised too.
: > "$work/notacommand"
chmod 644 "$work/notacommand"
curl_shadow=""
# /bin is usually a symlink to /usr/bin, but both are bound separately
# below, so both copies have to be covered.
[ "${GX_NO_CURL:-0}" = "1" ] && curl_shadow="--ro-bind $work/notacommand /usr/bin/curl --ro-bind $work/notacommand /bin/curl"

cat > "$work/bin/curl" <<'STUB'
#!/bin/sh
# Stands in for the real download.  Records every URL asked for so a test can
# assert the installer builds the addresses it is supposed to.
out=/dev/stdout
url=
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        http*) url=$1; shift ;;
        *) shift ;;
    esac
done
echo "curl $url" >> /gxlog/fetches
case $url in
    */releases/latest)
        printf '{\n  "tag_name": "v%s",\n  "name": "%s"\n}\n' \
            "$(cat /gxlog/latest)" "$(cat /gxlog/latest)" > "$out" ;;
    */archive/refs/tags/v*.tar.gz)
        tag=${url##*/tags/v}
        tag=${tag%.tar.gz}
        if [ -f "/gxfixtures/v$tag.tar.gz" ]; then
            cat "/gxfixtures/v$tag.tar.gz" > "$out"
        else
            echo "curl: (22) 404" >&2
            exit 22
        fi ;;
    *) echo "curl: (22) unexpected url $url" >&2; exit 22 ;;
esac
STUB

cat > "$work/bin/wget" <<'STUB'
#!/bin/sh
# BusyBox wget takes -O rather than -o; same fixtures underneath.
out=/dev/stdout
url=
while [ $# -gt 0 ]; do
    case $1 in
        -O) out=$2; shift 2 ;;
        http*) url=$1; shift ;;
        *) shift ;;
    esac
done
echo "wget $url" >> /gxlog/fetches
case $url in
    */releases/latest)
        printf '{\n  "tag_name": "v%s"\n}\n' "$(cat /gxlog/latest)" > "$out" ;;
    */archive/refs/tags/v*.tar.gz)
        tag=${url##*/tags/v}
        tag=${tag%.tar.gz}
        [ -f "/gxfixtures/v$tag.tar.gz" ] || exit 1
        cat "/gxfixtures/v$tag.tar.gz" > "$out" ;;
    *) exit 1 ;;
esac
STUB

# svc is daemontools' service control.  Recording the calls is how a test
# proves the installer only ever starts its own service.
cat > "$work/bin/svc" <<'STUB'
#!/bin/sh
echo "svc $*" >> /gxlog/svc
exit 0
STUB

# Answers captured from a real GX: can0 carries CAN-bus profile 3, which is
# "CAN-bus BMS LV (500 kbit/s)".  Anything else raises, as the real tool does.
cat > "$work/bin/dbus" <<'STUB'
#!/bin/sh
echo "dbus $*" >> /gxlog/dbus
case $* in
    *SetValue*|*SetDefault*|*AddSetting*)
        echo "the sandbox refuses a write-capable D-Bus call" >&2; exit 1 ;;
esac
case $* in
    *"/Settings/Canbus/can0/Profile"*GetValue*) echo 3; exit 0 ;;
    *"/Connected"*GetValue*) echo "value = 1"; exit 0 ;;
esac
echo "Traceback (most recent call last):" >&2
exit 1
STUB

cat > "$work/bin/candump" <<'STUB'
#!/bin/sh
echo "candump $*" >> /gxlog/candump
case $* in
    *can0*) ;;
    *) exit 1 ;;
esac
# A single Deye cycle, ids as the real candump prints them.
cat <<'FRAMES'
  can0  351   [8]  48 02 00 00 FC 08 E0 01
  can0  355   [8]  64 00 64 00 00 00 00 00
  can0  356   [8]  4A 15 00 00 E6 00 00 00
  can0  35E   [8]  50 59 4C 4F 4E 1C FC 08
FRAMES
STUB

cat > "$work/bin/ip" <<'STUB'
#!/bin/sh
case $* in
    *"link show can0"*|*"link show"*)
        cat <<'LINK'
3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 100
    link/can  promiscuity 0 allmulti 0 minmtu 0 maxmtu 0
    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 1000
	  bitrate 500000 sample-point 0.800
LINK
        ;;
    *) exit 1 ;;
esac
STUB

# Only so the suite does not spend real seconds in the installer's settle wait.
cat > "$work/bin/sleep" <<'STUB'
#!/bin/sh
exit 0
STUB

chmod 755 "$work"/bin/*
[ "${GX_NO_CURL:-0}" = "1" ] && rm -f "$work/bin/curl"

mkdir -p "$work/log"
: > "$work/log/fetches"
: > "$work/log/svc"
: > "$work/log/dbus"
: > "$work/log/candump"
echo "${GX_LATEST_VERSION:-$version}" > "$work/log/latest"

# Prefer a fully isolated sandbox.  Some CI hosts refuse to let an unprivileged
# namespace configure its own loopback ("Failed RTM_NEWADDR"), so fall back to
# keeping the host network -- every other namespace is still unshared, and the
# download stubs are what the tests assert on either way.
namespaces="--unshare-all"
if ! bwrap --unshare-all --ro-bind /usr /usr /usr/bin/true >/dev/null 2>&1; then
    namespaces="--unshare-user --unshare-ipc --unshare-pid --unshare-uts"
fi

exec bwrap \
    --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /sbin /sbin \
    --ro-bind-try /lib /lib --ro-bind-try /lib64 /lib64 --ro-bind /etc /etc \
    --dev /dev --proc /proc --tmpfs /tmp \
    --bind "$work/data" /data \
    --bind "$work/service" /service \
    --bind "$work/opt" /opt/victronenergy \
    --bind "$work/sysnet" /sys/class/net \
    --bind "$work/log" /gxlog \
    --ro-bind "$work/fixtures" /gxfixtures \
    --ro-bind "$work/bin" /gxbin \
    --ro-bind "$repo" /gxsrc \
    $curl_shadow \
    --setenv PATH "/gxbin:/usr/sbin:/usr/bin:/sbin:/bin" \
    $namespaces --uid 0 --gid 0 --die-with-parent \
    "$@"
