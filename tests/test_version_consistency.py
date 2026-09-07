"""Every version reference in the repository must agree.

pyproject.toml drifted to 0.7.2 while the package said 0.7.3, and the PC-only
mock model carried a hardcoded "0.3.0-shadow" from a much older release. Both
were silent: nothing asserted them. This test is why that cannot happen again.
"""

import re
from pathlib import Path

from deye_virtual_battery.cache import VirtualBatteryCache
from deye_virtual_battery.dbus_model import build_mock_dbus_model
from deye_virtual_battery.version import PACKAGE_VERSION, PROCESS_VERSION, VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_the_published_version_is_strictly_numeric():
    """Venus and VRM display /Mgmt/ProcessVersion verbatim."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION
    assert PROCESS_VERSION == VERSION
    assert PACKAGE_VERSION == VERSION


def test_pyproject_version_matches_the_package():
    declared = re.search(
        r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M
    )
    assert declared, "pyproject.toml has no version"
    assert declared.group(1) == VERSION


def test_no_stale_version_literal_survives_in_the_package():
    """A version literal that is not the current one is drift, by definition."""
    stale = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for found in re.findall(r'"(\d+\.\d+\.\d+)[^"]*"', line):
                if found != VERSION:
                    stale.append(f"{path.relative_to(ROOT)}:{number}: {found}")
    assert not stale, "stale version literals: " + "; ".join(stale)


def test_the_offline_mock_model_tracks_the_package_version():
    """The mock never registers on D-Bus, but it must not go stale either."""
    model = build_mock_dbus_model(VirtualBatteryCache().snapshot(at=0.0))
    assert model["paths"]["/Mgmt/ProcessVersion"] == f"{VERSION}-shadow"
