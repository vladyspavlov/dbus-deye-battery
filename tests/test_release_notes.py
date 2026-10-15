from pathlib import Path

import pytest

from tools.release_notes import section, unwrap


CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def test_wrapped_list_item_becomes_one_line():
    notes = unwrap(
        "### Fixed\n"
        "- **Bold** first line\n"
        "  continues here\n"
        "  and here.\n"
        "- Second item.\n"
    )
    assert notes == (
        "### Fixed\n"
        "- **Bold** first line continues here and here.\n"
        "- Second item."
    )


def test_paragraphs_headings_and_fences_keep_their_lines():
    source = (
        "### Added\n"
        "- Run it as\n"
        "\n"
        "  ```sh\n"
        "  first command\n"
        "  second command\n"
        "  ```\n"
        "\n"
        "  then read\n"
        "  the output.\n"
        "\n"
        "Plain paragraph\n"
        "wrapped.\n"
    )
    assert unwrap(source) == (
        "### Added\n"
        "- Run it as\n"
        "\n"
        "  ```sh\n"
        "  first command\n"
        "  second command\n"
        "  ```\n"
        "\n"
        "  then read the output.\n"
        "\n"
        "Plain paragraph wrapped."
    )


def test_nested_list_items_stay_separate():
    assert unwrap("- outer\n  - inner\n    wrapped\n1. numbered\n") == (
        "- outer\n  - inner wrapped\n1. numbered"
    )


def test_every_changelog_section_keeps_all_its_words():
    text = CHANGELOG.read_text(encoding="utf-8")
    versions = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
    assert versions
    for version in versions:
        raw = section(text, version)
        assert unwrap(raw).split() == raw.split(), version


def test_missing_version_is_an_error():
    with pytest.raises(LookupError):
        section(CHANGELOG.read_text(encoding="utf-8"), "0.0.0")
