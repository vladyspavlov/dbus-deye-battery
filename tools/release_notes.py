#!/usr/bin/env python3
"""Print one version's CHANGELOG.md section as GitHub release notes.

CHANGELOG.md is hard-wrapped for reading in a terminal.  GitHub renders a
release body like a comment, where every newline is a line break, so the
wrapped text would show as a narrow ragged column.  Continuation lines are
therefore joined onto the line they continue; list items, headings, quotes,
tables and fenced code keep their own lines.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

BLOCK_START = re.compile(r"\s*(?:[-*+]\s|\d+[.)]\s|#|>|\||```|~~~|<)")
FENCE = re.compile(r"\s*(?:```|~~~)")


def section(text: str, version: str) -> str:
    match = re.search(
        rf"^## {re.escape(version)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    if not match:
        raise LookupError(f"CHANGELOG.md has no '## {version}' section")
    return match.group(1).strip()


def unwrap(markdown: str) -> str:
    lines: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        previous = lines[-1] if lines else ""
        if (
            in_fence
            or not line.strip()
            or not previous.strip()
            or BLOCK_START.match(line)
            or FENCE.match(previous)
            or previous.lstrip().startswith(("#", "|"))
            or previous.endswith(("  ", "\\"))
        ):
            lines.append(line)
        else:
            lines[-1] = f"{previous} {line.strip()}"
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes.py VERSION", file=sys.stderr)
        return 2
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    try:
        print(unwrap(section(text, argv[1])))
    except LookupError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
