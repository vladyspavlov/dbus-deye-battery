"""The installer is the one file a stranger runs as root on a live inverter.

Two kinds of check live here.  The static ones read the scripts and assert the
things that must never appear in them.  The rest run the real bootstrap inside
a throwaway fake Venus OS built by tests/gxsim/gx-sandbox.sh, so the absolute
/data, /service and /opt/victronenergy paths are exercised as written without
any inverter being involved.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "tests" / "gxsim" / "gx-sandbox.sh"
BOOTSTRAP = (ROOT / "install" / "bootstrap.sh").read_text()
DETECT = (ROOT / "install" / "detect-can-interface.sh").read_text()
VERSION = re.search(
    r'^VERSION = "([^"]+)"', (ROOT / "src/deye_virtual_battery/version.py").read_text(), re.M
).group(1)

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or shutil.which("dash") is None,
    reason="the fake GX needs bubblewrap and dash",
)


def run_in_gx(script: str, version: str = VERSION, **env: str):
    """Run a shell script inside a fresh fake GX and return the result."""
    work = Path(tempfile.mkdtemp(prefix="gxsim-")) / "gx"
    environment = dict(os.environ, **env)
    try:
        return subprocess.run(
            [str(SANDBOX), str(work), str(ROOT), version, "/usr/bin/dash", "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
            env=environment,
        )
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


# --------------------------------------------------------------------------
# Static: what must never be in an installer for this system
# --------------------------------------------------------------------------

@pytest.mark.parametrize("script", [BOOTSTRAP, DETECT])
def test_the_installer_never_transmits_or_writes_to_the_system(script: str):
    for forbidden in ("cansend", "ip link set", "SetValue", "SetDefault",
                      "AddSetting", "killall", "pkill", "reboot"):
        assert forbidden not in script, f"{forbidden!r} has no business in an installer"


@pytest.mark.parametrize("script", [BOOTSTRAP, DETECT])
def test_the_installer_never_arms_can_transmit_or_selects_the_battery(script: str):
    # Both are deliberate operator actions with their own preconditions.
    assert "can-tx-enabled" not in script
    assert "handoff-can-owner" not in script
    assert "BatteryService" not in script


def test_bootstrap_survives_a_truncated_download():
    # Piped to a shell, a half-arrived script runs whatever arrived.  Keeping
    # every side effect inside functions and calling main() on the last line
    # means a truncated copy defines things and does nothing.
    assert BOOTSTRAP.rstrip().endswith('main "$@"')
    assert BOOTSTRAP.startswith("#!/bin/sh")
    assert "set -eu" in BOOTSTRAP


@pytest.mark.parametrize("name", ["bootstrap.sh", "detect-can-interface.sh"])
def test_installer_scripts_parse_as_posix_shell(name: str):
    subprocess.run(["sh", "-n", str(ROOT / "install" / name)], check=True)


@pytest.mark.parametrize("name", ["bootstrap.sh", "detect-can-interface.sh"])
def test_installer_scripts_parse_as_busybox_ash(name: str):
    # /bin/sh on a GX is BusyBox ash, so a bashism that dash tolerates is not
    # enough of a check.
    busybox = shutil.which("busybox")
    if busybox is None:
        pytest.skip("busybox is not installed")
    subprocess.run([busybox, "sh", "-n", str(ROOT / "install" / name)], check=True)


@pytest.mark.parametrize("readme", ["README.md", "README.uk.md"])
def test_both_readmes_document_the_same_one_liner(readme: str):
    text = (ROOT / readme).read_text()
    url = ("https://raw.githubusercontent.com/vladyspavlov/dbus-deye-battery/"
           "main/install/bootstrap.sh")
    assert url in text, f"{readme} does not show the bootstrap URL"
    assert "detect-can-interface.sh" in text


def test_bootstrap_refuses_a_machine_that_is_not_a_gx():
    # Run it on the machine running the tests, which has no /opt/victronenergy.
    if Path("/opt/victronenergy").exists():
        pytest.skip("this host looks like a GX")
    done = subprocess.run(
        ["sh", str(ROOT / "install" / "bootstrap.sh")],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode != 0
    assert "Venus OS" in done.stderr


# --------------------------------------------------------------------------
# Behavioural: the real script, in a fake GX
# --------------------------------------------------------------------------

@needs_sandbox
def test_a_fresh_install_lands_where_it_should_and_touches_nothing_else():
    done = run_in_gx(
        "MODEL=SE-F12-C dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---config"; cat /data/deye-virtual-battery/config; '
        'echo "---svc"; cat /gxlog/svc; '
        'echo "---dbus"; cat /gxlog/dbus; '
        'echo "---rclocal"; cat /data/rc.local; '
        'echo "---temp"; ls -d /data/.deye-bootstrap.* 2>/dev/null || echo none'
    )
    out = done.stdout
    assert "exit=0" in out, done.stderr
    assert f"verified: tag and packaged version both say {VERSION}" in out
    assert "MODEL=SE-F12-C" in out
    assert "CAN_INTERFACE=can0" in out
    # Only its own service, and only ever started -- never another one.
    for line in out.split("---svc")[1].split("---dbus")[0].strip().splitlines():
        assert line.endswith("/service/deye-virtual-battery"), line
    # Only reads on D-Bus.
    assert "GetValue" in out.split("---dbus")[1]
    assert "SetValue" not in out
    assert "none" in out.split("---temp")[1]


@needs_sandbox
def test_the_detector_is_left_at_a_stable_path_after_installing():
    done = run_in_gx(
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; "
        "dash /data/deye-virtual-battery/detect-can-interface.sh --quiet"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "can0"


@needs_sandbox
def test_it_detects_the_can_port_instead_of_assuming_one():
    done = run_in_gx("dash /gxsrc/install/detect-can-interface.sh")
    assert done.returncode == 0, done.stderr
    assert "Venus profile 3" in done.stdout
    assert "bitrate       500000" in done.stdout
    assert "Deye frames   yes" in done.stdout
    assert done.stdout.rstrip().endswith("Use CAN_INTERFACE=can0")


@needs_sandbox
def test_an_upgrade_keeps_the_running_systems_settings():
    later = "9.9.9"
    done = run_in_gx(
        # Install, deliberately move the config off the defaults, then upgrade.
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; "
        "sed -i 's|^CAN_INTERFACE=.*|CAN_INTERFACE=can1|' /data/deye-virtual-battery/config; "
        "sed -i 's|^SERVICE_NAME=.*|SERVICE_NAME=com.victronenergy.battery.deye_se_f12|' "
        "  /data/deye-virtual-battery/config; "
        f"VERSION={later} MODEL=IGNORED dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---config"; cat /data/deye-virtual-battery/config; '
        'echo "---backups"; ls -d /data/deye-virtual-battery/.backup-src-* ; '
        'echo "---version"; sed -n "s/^VERSION = .\\(.*\\)./\\1/p" '
        "  /data/deye-virtual-battery/src/deye_virtual_battery/version.py",
        GX_EXTRA_VERSIONS=later,
    )
    out = done.stdout
    assert "exit=0" in out, done.stderr
    config = out.split("---config")[1]
    assert "CAN_INTERFACE=can1" in config, "an upgrade moved the CAN port"
    assert "deye_se_f12" in config, "an upgrade changed the D-Bus identity"
    assert "IGNORED" not in config, "an upgrade overwrote a configured value"
    assert ".backup-src-" in out.split("---backups")[1]
    assert later in out.split("---version")[1]


@needs_sandbox
def test_it_refuses_to_downgrade_unless_told_to():
    older = "0.0.1"
    done = run_in_gx(
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; "
        f"VERSION={older} dash /gxsrc/install/bootstrap.sh >/gxlog/a 2>&1; "
        'echo "refused=$?"; cat /gxlog/a; '
        f"ALLOW_DOWNGRADE=1 VERSION={older} dash /gxsrc/install/bootstrap.sh >/gxlog/b 2>&1; "
        'echo "forced=$?"',
        GX_EXTRA_VERSIONS=older,
    )
    assert "refused=1" in done.stdout, done.stdout
    assert "Refusing to install" in done.stdout
    assert "forced=0" in done.stdout


@needs_sandbox
def test_a_wrong_checksum_stops_the_install():
    done = run_in_gx(
        "SHA256=" + "0" * 64 + " dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---"; ls /data/deye-virtual-battery 2>/dev/null || echo "nothing installed"'
    )
    assert "exit=1" in done.stdout
    assert "Checksum mismatch" in done.stdout
    assert "nothing installed" in done.stdout


@needs_sandbox
def test_a_tag_that_disagrees_with_its_code_is_rejected():
    done = run_in_gx(
        "VERSION=8.8.8 dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'ls /data/deye-virtual-battery 2>/dev/null || echo "nothing installed"',
        GX_EXTRA_VERSIONS="8.8.8",
        GX_MISLABELLED="8.8.8",
    )
    assert "exit=1" in done.stdout
    assert "Refusing to install a mislabelled release" in done.stdout
    assert "nothing installed" in done.stdout


@needs_sandbox
def test_it_works_piped_into_a_shell_with_only_busybox_wget():
    done = run_in_gx(
        "cat /gxsrc/install/bootstrap.sh | dash >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---fetch"; cat /gxlog/fetches',
        GX_NO_CURL="1",
    )
    assert "exit=0" in done.stdout, done.stdout
    fetches = done.stdout.split("---fetch")[1]
    assert "wget https://" in fetches
    assert "curl" not in fetches


@needs_sandbox
def test_dry_run_writes_nothing():
    done = run_in_gx(
        "DRY_RUN=1 dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---"; ls -A /data'
    )
    assert "exit=0" in done.stdout
    assert "nothing was written" in done.stdout
    assert "deye-virtual-battery" not in done.stdout.split("---")[-1]
