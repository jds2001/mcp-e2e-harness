"""Driver registry. The roster of supported drivers is an implementation property;
each driver's attribution capabilities must satisfy documentation/10-harness.md before
it may host attribution cells -- a driver that cannot verifiably close its own
non-server channels is refused, and the cell fails as instrument-broken rather than
running unattributable."""
from __future__ import annotations

from .base import Driver, DriverAttributionError, TurnSpec
from .claude_code import ClaudeCodeDriver
from .codex import CodexDriver

DRIVERS: dict[str, Driver] = {
    ClaudeCodeDriver.id: ClaudeCodeDriver(),
    CodexDriver.id: CodexDriver(),
}

__all__ = ["DRIVERS", "Driver", "DriverAttributionError", "TurnSpec"]
