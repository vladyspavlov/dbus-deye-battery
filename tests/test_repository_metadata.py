"""The repository's own names, versions and cross-links must agree.

The project has three legitimate names — the GitHub repository, the on-device
install root, and the Python package — and they drifted apart once already:
the README's title said one thing while the clone directory said another. The
install snippet also carries a literal version that no test watched. Both are
the kind of mistake nobody notices until a stranger follows the instructions.
"""

import re
from pathlib import Path

from deye_virtual_battery.version import VERSION


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "dbus-deye-battery"
DESCRIPTION = "Victron Venus OS battery driver for Deye SE-F LV packs over BMS-Can"
READMES = ("README.md", "README.uk.md")


# tomllib is 3.11+; the supported floor is 3.10, and a test guarding project
# metadata is not worth a dependency. These keys are one plain line each.
def pyproject(key: str) -> str:
    found = re.search(
        rf'^{key} = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(), re.M
    )
    assert found, f"pyproject.toml has no {key}"
    return found.group(1)


def project_urls() -> dict:
    block = re.search(
        r"^\[project\.urls\]\n((?:.+\n)+)", (ROOT / "pyproject.toml").read_text(), re.M
    )
    assert block, "pyproject.toml has no [project.urls]"
    return dict(re.findall(r'^(\w+) = "([^"]+)"$', block.group(1), re.M))


def test_the_distribution_is_named_after_the_repository():
    assert pyproject("name") == REPOSITORY


def test_every_project_url_points_at_that_repository():
    urls = project_urls()
    assert urls, "no URLs parsed"
    for name, url in urls.items():
        assert f"/{REPOSITORY}" in url, f"{name} -> {url}"


def test_one_description_is_used_everywhere():
    assert pyproject("description") == DESCRIPTION
    assert DESCRIPTION in (ROOT / "README.md").read_text()
    assert (ROOT / "NOTICE").read_text().startswith(REPOSITORY + "\n")


def test_the_install_snippet_pins_the_current_version():
    """A stale VERSION= sends people to a tag that does not exist yet."""
    for name in READMES:
        text = (ROOT / name).read_text()
        pinned = re.findall(r"^VERSION=(\S+)", text, re.M)
        assert pinned == [VERSION], f"{name}: {pinned}"
        assert f"cd {REPOSITORY}-$VERSION" in text, name


def test_the_translations_link_to_each_other():
    assert "(README.uk.md)" in (ROOT / "README.md").read_text()
    assert "(README.md)" in (ROOT / "README.uk.md").read_text()


def test_every_relative_link_in_a_readme_resolves():
    for name in READMES:
        for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", (ROOT / name).read_text()):
            assert (ROOT / target).exists(), f"{name} -> {target}"


def slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces to hyphens."""
    text = re.sub(r"^#+\s*", "", heading).strip().lower()
    text = re.sub(r"`|\*|_", "", text)
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def test_every_table_of_contents_anchor_resolves():
    for name in READMES:
        text = (ROOT / name).read_text()
        headings = {slug(line) for line in text.splitlines() if line.startswith("#")}
        for anchor in re.findall(r"\]\(#([^)]+)\)", text):
            assert anchor in headings, f"{name}: #{anchor} matches no heading"


def test_the_default_dbus_service_name_is_not_model_specific():
    """SE-F5 and SE-F16 packs run the same driver; the identity must not lie."""
    offenders = []
    for path in sorted(ROOT.glob("src/**/*.py")) + sorted(ROOT.glob("install/**/*")):
        if path.is_dir() or path.name == "install.sh":
            continue
        if "deye_se_f12" in path.read_text():
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_the_installer_preserves_a_pre_0_7_5_service_name():
    """Renaming the service on upgrade would deselect it as battery monitor."""
    text = (ROOT / "install/install.sh").read_text()
    assert "SERVICE_NAME=com.victronenergy.battery.deye_se_f12" in text
    assert 'grep -q \'^[[:space:]]*SERVICE_NAME=\' "$root/config"' in text
