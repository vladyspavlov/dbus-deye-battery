"""Regression tests for the v0.8.2 review in issue #15.

Each section names the finding it guards.  The tests reproduce the exact
failure the review described, so the same mistake cannot come back unnoticed.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
import time

import pytest

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.can_keepalive import (
    KeepaliveTransmitter,
    STOCK_DRIVER_EXE,
    StockDriverWatch,
    find_stock_driver,
    ownership_allowed,
)
from deye_virtual_battery.candump import parse_candump_line
from deye_virtual_battery.policy import (
    PolicyConfig,
    consistent_cell_counts,
    detect_cell_count,
    evaluate_policy,
)
from deye_virtual_battery.venus_runtime import VenusPublisherCore, encode_socketcan_frame
from deye_virtual_battery.watchdog import StallWatchdog
from deye_virtual_battery import venus_bms_publisher as publisher

from test_installer import VERSION, needs_sandbox, run_in_gx

ROOT = Path(__file__).resolve().parents[1]
ROLLBACK = ROOT / "install" / "service" / "rollback-can-owner"


def readings(pack_v: float, minimum: float, maximum: float) -> dict:
    return {
        "battery.voltage": {"effective_value": pack_v},
        "cells.min_voltage_200": {"effective_value": minimum},
        "cells.max_voltage_200": {"effective_value": maximum},
    }


def sixteen_cells(rest: float, odd: float) -> dict:
    """A 16s pack: fifteen cells at ``rest`` and one at ``odd``."""
    return readings(round(15 * rest + odd, 3), min(rest, odd), max(rest, odd))


# --------------------------------------------------------------------------
# H1: the series count must never be guessed from an ambiguous reading
# --------------------------------------------------------------------------

WEAK_CELL_CASES = [
    # (cells at, one cell at): every one of these used to be detected as 17s
    (3.40, 3.00),
    (3.45, 3.10),
    (3.35, 3.10),
    (3.20, 2.80),
    (3.30, 3.05),
]


@pytest.mark.parametrize("rest,weak", WEAK_CELL_CASES)
def test_one_weak_cell_does_not_turn_a_16s_pack_into_17s(rest, weak):
    fields = sixteen_cells(rest, weak)
    pack = fields["battery.voltage"]["effective_value"]
    # The trap: more than one count fits the bracket, and 16 is one of them.
    assert {16, 17} <= set(consistent_cell_counts(pack, weak, rest))
    assert detect_cell_count(fields, 16) == (16, False)
    config = PolicyConfig().for_pack(fields)
    assert config.cell_count == 16
    assert config.provisional_charge_ceiling_v == 57.2
    assert config.blocked_charge_cvl_v == 55.2
    assert config.pack_overvoltage_protection_v == 58.4


def test_the_audits_own_example_is_not_17s():
    assert detect_cell_count(readings(54.0, 2.80, 3.60), 16) == (16, False)


@pytest.mark.parametrize("count", [8, 15, 16, 20, 24])
def test_a_healthy_pack_of_any_size_is_still_measured(count):
    fields = readings(round(count * 3.33, 3), 3.32, 3.34)
    assert detect_cell_count(fields, 16) == (count, True)


def cache_with_cells(*, pack_v: float, minimum: float, maximum: float, charging: bool):
    def frame(can_id: str, payload: str, timestamp: float):
        return parse_candump_line(f"({timestamp:.6f}) can0 {can_id}#{payload} R")

    def cells(maximum: float, minimum: float) -> str:
        return struct.pack("<HHHH", round(maximum * 1000), round(minimum * 1000), 230, 230).hex()

    measurements = struct.pack("<hhh", round(pack_v * 100), 0, 230).hex() + "0000"
    if charging:
        frame_110, frame_351, frame_371 = "0000000000000031", "48024408FC08E001", "4408FC0800000000"
    else:
        frame_110, frame_351, frame_371 = "0000000000000021", "48020000FC08E001", "0000FC0800000000"
    cache = VirtualBatteryCache()
    cache.apply_all(
        (
            frame("110", frame_110, 1000.00),
            frame("200", cells(maximum, minimum), 1000.01),
            frame("351", frame_351, 1000.02),
            frame("355", "6400640000000000", 1000.03),
            frame("356", measurements, 1000.04),
            frame("359", "0000000000000000", 1000.05),
            frame("35C", "4000000000000000", 1000.06),
            frame("35E", "44593030311CFC08", 1000.07),
            frame("361", cells(maximum, minimum), 1000.08),
            frame("364", "0101000001000000", 1000.09),
            frame("371", frame_371, 1000.10),
            frame("400", "0000000000001200", 1000.11),
        )
    )
    return cache


@pytest.mark.parametrize("charging,expected_cvl", [(True, 57.2), (False, 55.2)])
def test_the_raw_58_4_v_cvl_never_passes_through_on_a_weak_cell(charging, expected_cvl):
    """The review's headline: 58.4 V was published with CCL 35 A and with CCL 0."""
    cache = cache_with_cells(pack_v=15 * 3.35 + 3.10, minimum=3.10, maximum=3.35, charging=charging)
    policy = evaluate_policy(cache.snapshot(at=1000.2))
    assert policy["critical_data_ready"] is True
    assert policy["raw_limits"]["cvl_v"] == 58.4
    assert policy["effective_limits"]["cvl_v"] == expected_cvl


def test_a_configured_cell_count_is_not_overridden_by_measurement():
    """CELL_COUNT used to be only a fallback; the heuristic stayed active."""
    fifteen_looking = readings(51.0, 3.39, 3.41)
    assert detect_cell_count(fifteen_looking, 16) == (15, True)
    configured = PolicyConfig().with_cell_count(16)
    bound = configured.for_pack(fifteen_looking)
    assert bound.cell_count == 16
    assert bound.provisional_charge_ceiling_v == 57.2


def test_the_count_measured_at_qualification_is_kept():
    locked = PolicyConfig().locked_to_pack(readings(53.28, 3.32, 3.34))
    assert locked.cell_count == 16 and locked.cell_count_fixed
    for later in (readings(51.0, 3.39, 3.41), sixteen_cells(3.35, 3.10), {}):
        assert locked.for_pack(later).cell_count == 16


def test_a_count_that_cannot_be_measured_at_qualification_is_not_locked():
    unlocked = PolicyConfig().locked_to_pack(sixteen_cells(3.35, 3.10))
    assert unlocked.cell_count == 16 and not unlocked.cell_count_fixed
    # It may still be measured later from an unambiguous reading.
    assert unlocked.for_pack(readings(51.0, 3.39, 3.41)).cell_count == 15


def test_the_publisher_hands_the_locked_count_to_the_running_policy():
    core = VenusPublisherCore()
    locked = PolicyConfig().locked_to_pack(readings(53.28, 3.32, 3.34))
    publisher._apply_policy_config(core, locked)
    assert core.policy_config is locked
    assert core.shadow.policy_config is locked


def test_the_cell_count_option_pins_the_count():
    source = Path(publisher.__file__).read_text()
    assert "config.with_cell_count(arguments.cell_count)" in source
    assert "config.locked_to_pack(" in source


# --------------------------------------------------------------------------
# H3: a stalled publisher must stop vouching for its limits
# --------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_watchdog_fires_once_when_the_loop_goes_quiet():
    clock = Clock()
    fired: list[float] = []
    watchdog = StallWatchdog(timeout_seconds=30, on_stall=fired.append, clock=clock)
    clock.now = 29.0
    assert watchdog.check() and watchdog.alive()
    watchdog.beat()
    clock.now = 58.0
    assert watchdog.check() and fired == []
    clock.now = 60.0
    assert not watchdog.check()
    assert not watchdog.alive()
    assert not watchdog.check()
    assert fired == [31.0]
    # Once fired the process is leaving; a late beat does not revive TX.
    watchdog.beat()
    assert not watchdog.alive()


def test_the_watchdog_thread_calls_the_stall_handler():
    fired = threading.Event()
    watchdog = StallWatchdog(timeout_seconds=0.05, on_stall=lambda _: fired.set(), poll_seconds=0.01)
    watchdog.start()
    try:
        assert fired.wait(2.0)
    finally:
        watchdog.stop()


def test_ownership_requires_a_live_publisher():
    assert ownership_allowed(feature_enabled=True, armed=True, stock_running=False)
    assert not ownership_allowed(
        feature_enabled=True, armed=True, stock_running=False, publisher_alive=False
    )


class FakeTxSocket:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def send(self, raw: bytes) -> int:
        self.frames.append(raw)
        return len(raw)

    def close(self) -> None:
        pass


def test_the_keepalive_stops_as_soon_as_the_publisher_stalls(tmp_path: Path):
    arm_file = tmp_path / "tx-armed"
    arm_file.touch()
    alive = {"value": True}
    fake = FakeTxSocket()
    transmitter = KeepaliveTransmitter(
        interface="can0",
        feature_enabled=True,
        arm_file=arm_file,
        stock_detector=lambda fresh=False: False,
        socket_factory=lambda _: fake,
        period_seconds=0.01,
        poll_seconds=0.002,
        publisher_alive=lambda: alive["value"],
    )
    transmitter.start()
    try:
        deadline = time.monotonic() + 1.0
        while len(fake.frames) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert fake.frames, "a live publisher should be allowed to transmit"
        alive["value"] = False
        time.sleep(0.03)
        sent = len(fake.frames)
        time.sleep(0.08)
        assert len(fake.frames) == sent
        assert transmitter.snapshot().owner is False
    finally:
        transmitter.stop()


def test_the_publisher_wires_the_watchdog_into_the_keepalive():
    source = Path(publisher.__file__).read_text()
    assert "publisher_alive=watchdog.alive" in source
    assert "watchdog.beat()" in source


def test_every_dbus_read_has_a_bounded_timeout():
    seen: list[dict] = []

    class Item:
        def GetValue(self, **kwargs):
            seen.append(kwargs)
            return 3

    class Bus:
        def get_object(self, service, path):
            return Item()

    assert publisher._get_value(Bus(), "com.victronenergy.settings", "/X") == 3
    assert seen[0]["timeout"] == publisher.DBUS_CALL_TIMEOUT_SECONDS
    assert 0 < publisher.DBUS_CALL_TIMEOUT_SECONDS < 25


# --------------------------------------------------------------------------
# H6: frames carry the time they were received, and a drain is bounded
# --------------------------------------------------------------------------


def test_a_backlog_is_aged_by_its_kernel_receive_time():
    assert publisher.frame_timestamp(990.0, now=5000.0, wall_now=1000.0) == 4990.0
    assert publisher.frame_timestamp(None, now=5000.0, wall_now=1000.0) == 5000.0
    # A realtime step backwards cannot make a frame newer than now.
    assert publisher.frame_timestamp(1010.0, now=5000.0, wall_now=1000.0) == 5000.0
    # Nor can a step forwards age it without bound.
    assert publisher.frame_timestamp(0.0, now=5000.0, wall_now=1e9) == (
        5000.0 - publisher.MAX_FRAME_AGE_SECONDS
    )


def timeval(seconds: float) -> bytes:
    whole = int(seconds)
    return struct.pack(publisher.TIMEVAL_FORMAT, whole, round((seconds - whole) * 1e6))


class FakeCanSocket:
    def __init__(self, frames: list[tuple[bytes, float | None]]) -> None:
        self.frames = list(frames)

    def recvmsg(self, size, ancillary_size):
        if not self.frames:
            raise BlockingIOError
        raw, received = self.frames.pop(0)
        ancillary = []
        if received is not None:
            ancillary.append((socket.SOL_SOCKET, publisher.SO_TIMESTAMP, timeval(received)))
        return raw, ancillary, 0, None


class RecordingCore:
    def __init__(self) -> None:
        self.stamps: list[float] = []

    def apply_raw_frame(self, raw, *, timestamp):
        self.stamps.append(timestamp)


def test_queued_frames_are_not_taken_for_fresh_data():
    """After a pause the queue used to be stamped with the time it was read."""
    raw = encode_socketcan_frame(0x351, bytes(8))
    sock = FakeCanSocket([(raw, 980.0), (raw, 999.5), (raw, None)])
    core = RecordingCore()
    count = publisher._drain_can(sock, core, lambda: 5000.0, wall_time=lambda: 1000.0)
    assert count == 3
    assert core.stamps == [4980.0, 4999.5, 5000.0]


def test_one_drain_reads_a_bounded_number_of_frames():
    raw = encode_socketcan_frame(0x351, bytes(8))
    sock = FakeCanSocket([(raw, None)] * 5)
    core = RecordingCore()
    assert publisher._drain_can(sock, core, lambda: 1.0, max_frames=2) == 2
    assert len(sock.frames) == 3


def test_the_passive_socket_asks_the_kernel_for_receive_times():
    source = Path(publisher.__file__).read_text()
    assert "setsockopt(socket.SOL_SOCKET, SO_TIMESTAMP, 1)" in source


# --------------------------------------------------------------------------
# E2: a stock driver on another CAN port does not block this one
# --------------------------------------------------------------------------


def fake_process(proc: Path, pid: int, *, exe: str = STOCK_DRIVER_EXE,
                 cwd: str | None = None, args: list[str] | None = None) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    os.symlink(exe, directory / "exe")
    if cwd is not None:
        os.symlink(cwd, directory / "cwd")
    (directory / "cmdline").write_bytes(
        b"\0".join(arg.encode() for arg in [exe, *(args or [])]) + b"\0"
    )


def test_a_stock_driver_is_attributed_to_its_port_by_service_directory(tmp_path: Path):
    proc = tmp_path / "proc"
    fake_process(proc, 100, cwd="/opt/victronenergy/service/can-bus-bms.can1")
    assert find_stock_driver(interface="can0", proc_root=str(proc)) == (False, None)
    assert find_stock_driver(interface="can1", proc_root=str(proc)) == (True, "100")


def test_a_stock_driver_is_attributed_to_its_port_by_arguments(tmp_path: Path):
    proc = tmp_path / "proc"
    fake_process(proc, 200, args=["-c", "socketcan:can1"])
    assert find_stock_driver(interface="can0", proc_root=str(proc))[0] is False
    assert find_stock_driver(interface="can1", proc_root=str(proc))[0] is True


def test_an_unattributable_stock_driver_still_blocks_tx(tmp_path: Path):
    proc = tmp_path / "proc"
    fake_process(proc, 300)
    assert find_stock_driver(interface="can0", proc_root=str(proc)) == (True, "300")


def test_other_executables_are_ignored(tmp_path: Path):
    proc = tmp_path / "proc"
    fake_process(proc, 400, exe="/usr/bin/python3", cwd="/service/can-bus-bms.can0")
    assert find_stock_driver(interface="can0", proc_root=str(proc)) == (False, None)


def test_the_watch_used_by_the_transmitter_is_per_interface(tmp_path: Path):
    proc = tmp_path / "proc"
    fake_process(proc, 100, cwd="/service/can-bus-bms.can1")
    assert StockDriverWatch(interface="can0", proc_root=str(proc))(fresh=True) is False
    assert StockDriverWatch(interface="can1", proc_root=str(proc))(fresh=True) is True
    transmitter = KeepaliveTransmitter(interface="can1", feature_enabled=False)
    assert transmitter.stock_detector.interface == "can1"


# --------------------------------------------------------------------------
# E1 and E2: rollback must restore the stock driver even if the adapter is gone
# --------------------------------------------------------------------------


def rollback_environment(tmp_path: Path, *, owner: str | None) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answer = (
        'echo "Traceback (most recent call last):" >&2; exit 1'
        if owner is None
        else f'case "$*" in *Owner*) echo {owner} ;; *) echo 1 ;; esac'
    )
    (bin_dir / "dbus").write_text(f"#!/bin/sh\n{answer}\n")
    (bin_dir / "svc").write_text(f'#!/bin/sh\necho "svc $*" >> "{tmp_path}/svc.log"\n')
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()
    (root / "can-tx-enabled").touch()
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "tx-armed").touch()
    return dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEYE_ROOT=str(root),
        DEYE_RUNTIME_DIR=str(runtime),
        DEYE_PROC_ROOT=str(tmp_path / "proc"),
    )


def run_rollback(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(ROLLBACK)], env=env, capture_output=True, text=True, timeout=20
    )


def svc_calls(tmp_path: Path) -> str:
    log = tmp_path / "svc.log"
    return log.read_text() if log.exists() else ""


def test_rollback_restores_the_stock_driver_when_the_adapter_is_not_on_dbus(tmp_path: Path):
    env = rollback_environment(tmp_path, owner=None)
    fake_process(tmp_path / "proc", 100, cwd="/service/can-bus-bms.can0")
    done = run_rollback(env)
    assert done.returncode == 0, done.stderr
    assert "svc -u /service/can-bus-bms.can0" in svc_calls(tmp_path)
    assert not (Path(env["DEYE_ROOT"]) / "can-tx-enabled").exists()
    assert not (Path(env["DEYE_RUNTIME_DIR"]) / "tx-armed").exists()


def test_rollback_completes_normally_when_the_adapter_lets_go(tmp_path: Path):
    env = rollback_environment(tmp_path, owner="0")
    fake_process(tmp_path / "proc", 100, cwd="/service/can-bus-bms.can0")
    done = run_rollback(env)
    assert done.returncode == 0, done.stderr
    assert "rollback complete" in done.stdout


def test_rollback_refuses_while_the_adapter_still_claims_the_bus(tmp_path: Path):
    env = rollback_environment(tmp_path, owner="1")
    done = run_rollback(env)
    assert done.returncode == 4
    assert svc_calls(tmp_path) == ""


def test_rollback_does_not_mistake_another_ports_driver_for_its_own(tmp_path: Path):
    env = rollback_environment(tmp_path, owner="0")
    fake_process(tmp_path / "proc", 100, cwd="/service/can-bus-bms.can1")
    done = run_rollback(env)
    assert done.returncode == 5
    assert "did not return" in done.stderr


# --------------------------------------------------------------------------
# E1, S1, S2, S3: the installer, in the fake GX
# --------------------------------------------------------------------------


@needs_sandbox
def test_an_upgrade_restarts_the_running_driver():
    later = "9.9.9"
    done = run_in_gx(
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; : > /gxlog/svc; "
        f"VERSION={later} dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; echo "---svc"; cat /gxlog/svc; '
        'echo "---backup"; ls /data/deye-virtual-battery/.backup-service-*/',
        GX_EXTRA_VERSIONS=later,
    )
    out = done.stdout
    assert "exit=0" in out, done.stderr
    assert "svc -t /service/deye-virtual-battery" in out.split("---svc")[1]
    backup = out.split("---backup")[1]
    assert "run" in backup and "config" in backup


@needs_sandbox
def test_an_upgrade_can_defer_the_restart():
    later = "9.9.9"
    done = run_in_gx(
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; : > /gxlog/svc; "
        f"RESTART_SERVICE=0 VERSION={later} dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; echo "---svc"; cat /gxlog/svc',
        GX_EXTRA_VERSIONS=later,
    )
    out = done.stdout
    assert "exit=0" in out, done.stderr
    assert "svc -t" not in out.split("---svc")[1]
    assert "keeps the old code" in out


@needs_sandbox
def test_a_reinstall_uses_the_verified_download_not_the_copy_on_disk():
    kept = f"/data/dbus-deye-battery-{VERSION}"
    done = run_in_gx(
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; "
        f"echo 'echo TAMPERED-INSTALLER' >> {kept}/install/install.sh; "
        f"echo 'TAMPERED = 1' >> {kept}/src/deye_virtual_battery/policy.py; "
        "dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---installed"; grep -c TAMPERED '
        "/data/deye-virtual-battery/src/deye_virtual_battery/policy.py || true; "
        f'echo "---kept"; grep -c TAMPERED {kept}/install/install.sh || true; '
        f'echo "---leftover"; ls -d {kept}.new 2>/dev/null || echo none'
    )
    out = done.stdout
    assert "exit=0" in out, done.stderr
    assert "TAMPERED-INSTALLER" not in out.split("---installed")[0]
    assert out.split("---installed")[1].split("---kept")[0].strip() == "0"
    assert out.split("---kept")[1].split("---leftover")[0].strip() == "0"
    assert "none" in out.split("---leftover")[1]


HOSTILE_INPUTS = [
    ("MODEL", "x'; touch /data/pwned; '"),
    ("MODEL", "a|b"),
    ("MODEL", "a&b"),
    ("CAN_INTERFACE", "can0; touch /data/pwned"),
    ("DEVICE_INSTANCE", "513'"),
    ("AUTO_DEVICE_INSTANCE", "2"),
    ("SERVICE_NAME", "com.victronenergy.battery.x$(id)"),
    ("VERSION", "0.8.2/../../x"),
    ("SHA256", "not-hex"),
]


@needs_sandbox
@pytest.mark.parametrize("key,value", HOSTILE_INPUTS)
def test_unsafe_settings_are_refused_before_anything_is_written(key, value):
    quoted = value.replace("'", "'\\''")
    done = run_in_gx(
        f"{key}='{quoted}' dash /gxsrc/install/bootstrap.sh >/gxlog/out 2>&1; "
        'echo "exit=$?"; cat /gxlog/out; '
        'echo "---"; ls /data/deye-virtual-battery 2>/dev/null || echo "nothing installed"; '
        "ls /data/pwned 2>/dev/null || echo clean"
    )
    out = done.stdout
    assert "exit=1" in out, out
    assert "nothing installed" in out
    assert "clean" in out


@needs_sandbox
def test_the_installed_tree_is_not_writable_by_others():
    done = run_in_gx(
        "dash /gxsrc/install/bootstrap.sh >/dev/null 2>&1; "
        "find /data/deye-virtual-battery -perm -o+w -o -perm -g+w | grep -v /supervise || echo none"
    )
    assert done.stdout.strip().splitlines()[-1] == "none", done.stdout


def test_temporary_names_are_not_predictable():
    bootstrap = (ROOT / "install" / "bootstrap.sh").read_text()
    detect = (ROOT / "install" / "detect-can-interface.sh").read_text()
    assert ".deye-bootstrap.$$" not in bootstrap
    assert 'mktemp -d "$BASE/.deye-bootstrap.XXXXXX"' in bootstrap
    assert "/tmp/deye-detect-$interface.$$" not in detect
    assert "mktemp" in detect
