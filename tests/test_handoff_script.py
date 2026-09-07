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
