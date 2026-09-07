"""Parser for the timestamped candump format used by the recorder."""

from __future__ import annotations

from dataclasses import dataclass
import re


LINE_PATTERN = re.compile(
    r"^\((?P<timestamp>\d+(?:\.\d+)?)\)\s+"
    r"(?P<interface>\S+)\s+"
    r"(?P<can_id>[0-9A-Fa-f]{1,8})#(?P<payload>[0-9A-Fa-f]*)"
    r"(?:\s+(?P<direction>[RT]))?\s*$"
)


class CandumpParseError(ValueError):
    """Raised when a line is not a supported candump data frame."""


@dataclass(frozen=True, slots=True)
class CanFrame:
    timestamp: float
    interface: str
    can_id: int
    payload: bytes
    direction: str | None
    original: str

    @property
    def dlc(self) -> int:
        return len(self.payload)


def parse_candump_line(line: str) -> CanFrame:
    original = line.rstrip("\r\n")
    match = LINE_PATTERN.match(original)
    if not match:
        raise CandumpParseError("unsupported or malformed candump line")
    payload_text = match.group("payload")
    if len(payload_text) % 2:
        raise CandumpParseError("CAN payload contains an odd number of hex digits")
    if len(payload_text) > 16:
        raise CandumpParseError("Classical CAN payload exceeds eight bytes")
    try:
        payload = bytes.fromhex(payload_text)
    except ValueError as error:
        raise CandumpParseError("CAN payload is not hexadecimal") from error
    return CanFrame(
        timestamp=float(match.group("timestamp")),
        interface=match.group("interface"),
        can_id=int(match.group("can_id"), 16),
        payload=payload,
        direction=match.group("direction"),
        original=original,
    )
