import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "install" / "service" / "handoff-can-owner"


def test_handoff_refuses_frozen_publisher_heartbeat(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dbus = fake_bin / "dbus"
    dbus.write_text(
        """#!/bin/sh
# "dbus -y" with no further arguments lists the services on the bus, which is
# how the script discovers VE.Bus.
if [ "$*" = "-y" ]; then
  echo "com.victronenergy.vebus.ttyO2"
  exit 0
fi
case "$*" in
  *BmsInstance*|*ActiveBmsInstance*) echo 513 ;;
  *FeatureEnabled*|*/Connected*|*CanAlive*|*AllowToDischarge*) echo 1 ;;
  *Lifecycle/State*) echo "'online'" ;;
  *CriticalDataStale*|*HighDischargeCurrent*|*InternalFailure*|*VebusError*|*LowBattery*) echo 0 ;;
  *Lifecycle/CanAge*) echo 0.1 ;;
  *Publisher/Heartbeat*) echo 42 ;;
  *MaxDischargeCurrent*) echo 230 ;;
  */Mode*) echo 3 ;;
  *Ac/Out/L1/V*) echo 230 ;;
  *) echo 0 ;;
esac
"""
    )
    dbus.chmod(0o755)
    svc = fake_bin / "svc"
    svc.write_text("#!/bin/sh\nexit 99\n")
    svc.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        ["/bin/sh", str(HANDOFF)],
        text=True,
        capture_output=True,
        timeout=5,
        env=environment,
        check=False,
    )

    assert result.returncode == 3
    assert "fresh, progressing" in result.stderr


def _fake_bin(tmp_path: Path, vebus_listing: str) -> Path:
    """A stub GX environment whose D-Bus reports every precondition as ready."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dbus = fake_bin / "dbus"
    dbus.write_text(
        f"""#!/bin/sh
if [ "$*" = "-y" ]; then
{vebus_listing}
  exit 0
fi
case "$*" in
  *BmsInstance*|*ActiveBmsInstance*) echo 513 ;;
  *FeatureEnabled*|*/Connected*|*CanAlive*|*AllowToDischarge*) echo 1 ;;
  *Lifecycle/State*) echo "'online'" ;;
  *CriticalDataStale*|*HighDischargeCurrent*|*InternalFailure*|*VebusError*|*LowBattery*) echo 0 ;;
  *Lifecycle/CanAge*) echo 0.1 ;;
  *Publisher/Heartbeat*) echo 42 ;;
  *MaxDischargeCurrent*) echo 230 ;;
  */Mode*) echo 3 ;;
  *Ac/Out/L1/V*) echo 230 ;;
  *) echo 0 ;;
esac
"""
    )
    dbus.chmod(0o755)
    svc = fake_bin / "svc"
    svc.write_text("#!/bin/sh\nexit 99\n")
    svc.chmod(0o755)
    return fake_bin


def _run(tmp_path: Path, vebus_listing: str):
    environment = os.environ.copy()
    environment["PATH"] = f"{_fake_bin(tmp_path, vebus_listing)}:{environment['PATH']}"
    return subprocess.run(
        ["/bin/sh", str(HANDOFF)],
        text=True,
        capture_output=True,
        timeout=5,
        env=environment,
        check=False,
    )


def test_handoff_refuses_when_no_vebus_service_is_present(tmp_path: Path):
    """Without VE.Bus there is no AC-output check, so taking CAN is unsafe."""
    result = _run(tmp_path, "  :")

    assert result.returncode == 4
    assert "no VE.Bus service found" in result.stderr


def test_handoff_discovers_vebus_on_a_port_it_was_not_developed_against(
    tmp_path: Path,
):
    """The port differs between GX models; it must never be assumed."""
    result = _run(tmp_path, '  echo "com.victronenergy.vebus.ttyUSB0"')

    # It gets past discovery and fails later, on the frozen heartbeat, rather
    # than failing at discovery with exit 4.
    assert result.returncode == 3, result.stderr
    assert "no VE.Bus service found" not in result.stderr


def _stateful_gx(tmp_path: Path, *, driver_polls_marker: bool):
    """A stub GX whose answers follow the files the handoff creates.

    The handoff's absolute /data and /run paths are redirected into tmp_path in
    a copy of the script, so the marker and arm file it writes can be observed.
    """
    root = tmp_path / "data"
    run = tmp_path / "run"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    script = tmp_path / "handoff-can-owner"
    script.write_text(
        HANDOFF.read_text()
        .replace("root=/data/deye-virtual-battery", f"root={root}")
        .replace("runtime_dir=/run/deye-virtual-battery", f"runtime_dir={run}")
    )
    feature = (
        f'[ -f "{root}/can-tx-enabled" ] && echo 1 || echo 0'
        if driver_polls_marker
        else "echo 0"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dbus = fake_bin / "dbus"
    dbus.write_text(
        f"""#!/bin/sh
if [ "$*" = "-y" ]; then
  echo "com.victronenergy.vebus.ttyS3"
  exit 0
fi
owner() {{ [ -f "{run}/tx-armed" ] && [ -f "{state}/stock-down" ]; }}
case "$*" in
  *FeatureEnabled*) {feature} ;;
  *CanTx/Owner*) owner && echo 1 || echo 0 ;;
  *StockDriverRunning*) owner && echo 0 || echo 1 ;;
  *PairsSent*) owner && echo 3 || echo 0 ;;
  *CanTx/Errors*) echo 0 ;;
  *Publisher/Heartbeat*)
    beat=$(($(cat "{state}/beat" 2>/dev/null || echo 0) + 1))
    echo "$beat" > "{state}/beat"
    echo "$beat" ;;
  *BmsInstance*|*ActiveBmsInstance*) echo 513 ;;
  */Connected*|*CanAlive*|*AllowToDischarge*) echo 1 ;;
  *Lifecycle/State*) echo "'online'" ;;
  *CriticalDataStale*|*HighDischargeCurrent*|*InternalFailure*|*VebusError*|*LowBattery*) echo 0 ;;
  *Lifecycle/CanAge*) echo 0.1 ;;
  *MaxDischargeCurrent*) echo 230 ;;
  */Mode*) echo 3 ;;
  *Ac/Out/L1/V*) echo 230 ;;
  *) echo 0 ;;
esac
"""
    )
    dbus.chmod(0o755)
    svc = fake_bin / "svc"
    svc.write_text(
        f"""#!/bin/sh
echo "$*" >> "{state}/svc-calls"
case "$1" in
  -d) touch "{state}/stock-down" ;;
  -u) rm -f "{state}/stock-down" ;;
esac
"""
    )
    svc.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    result = subprocess.run(
        ["/bin/sh", str(script)],
        text=True,
        capture_output=True,
        timeout=15,
        env=environment,
        check=False,
    )
    return result, root, run, state


def test_handoff_completes_on_a_fresh_install_without_the_marker(tmp_path: Path):
    """Issue #2: the marker must not be a precondition of the step that creates it."""
    result, root, run, state = _stateful_gx(tmp_path, driver_polls_marker=True)

    assert result.returncode == 0, result.stderr
    assert "handoff complete" in result.stdout
    assert (root / "can-tx-enabled").is_file()
    assert (run / "tx-armed").is_file()
    assert (state / "svc-calls").read_text().split("\n")[0].startswith("-d ")


def test_handoff_keeps_the_stock_driver_when_the_driver_cannot_enable_tx(
    tmp_path: Path,
):
    """An older driver that read the marker only at startup must not be handed CAN."""
    result, root, run, state = _stateful_gx(tmp_path, driver_polls_marker=False)

    assert result.returncode == 3
    assert "feature enabled" in result.stderr
    assert not (root / "can-tx-enabled").exists()
    assert not (run / "tx-armed").exists()
    assert not (state / "svc-calls").exists()
