"""Driver interface.

The driver is part of the instrument: two cells that differ in driver also differ in
system prompt, tool-call formatting, and client-side behavior the trace cannot see, so
knobs are recorded in the driver's native vocabulary, verbatim, never translated
across vendors (documentation/10-harness.md, run mechanics).
"""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class DriverAttributionError(RuntimeError):
    """The argv about to run does not verifiably close the channels it claims to.

    Raised before anything is spent; the cell is instrument-broken, never run
    unattributable."""


@dataclass(frozen=True)
class TurnSpec:
    """One consumer turn: what to execute and how to read the answer back."""
    argv: list[str]
    stdin_text: str
    # Where the final answer comes from: "stdout", or a file path the driver writes.
    answer_from: str = "stdout"
    answer_path: Path | None = None


@dataclass(frozen=True)
class TurnContext:
    model: str
    knobs: dict
    mcp_config_path: Path
    server_name: str
    tool_surface: object  # "full" or list[str]
    extra_server_names: tuple[str, ...] = ()
    # (mode, session id): mode is "single" (no session), "open" (first turn of a
    # session, for crowding pre-turns), or "resume" (a later turn of that session).
    session: tuple[str, str] = ("single", "")
    prompt: str = ""
    dest: Path = field(default_factory=Path)


class Driver(ABC):
    id: str
    executable: str
    # Builtin tool names this driver's argv disallows; the builtin-surface probe (Q2)
    # checks the consumer's self-reported surface against exactly these names.
    disallowed_builtins: tuple[str, ...] = ()
    # Default model for the builtin-surface probe when none is given, in the driver's
    # own vocabulary. Empty means the driver cannot be probed, and therefore may not
    # host merge-gating cells (the probe gates them).
    probe_model: str = ""
    # Env var that redirects this driver's outbound API traffic (e.g.
    # ANTHROPIC_BASE_URL). Non-empty enables the API-boundary tool-surface capture --
    # Q2's preferred instrument, wire-level ground truth recorded on every scored
    # invocation. Empty means capture is unsupported and merge-gating cells fall back
    # to the calibration probe.
    api_base_env: str = ""
    # Where that traffic goes when the harness's own environment does not already
    # override api_base_env.
    api_default_upstream: str = ""

    def cli_version(self) -> str:
        """The driver CLI's version string, recorded at run time -- never assumed.

        Two runs of "the same" cell under different CLI versions are different
        instruments, and the difference is invisible unless captured when it is true.
        """
        try:
            out = subprocess.run([self.executable, "--version"], capture_output=True,
                                 text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"UNKNOWN (--version failed: {type(exc).__name__})"
        first = ((out.stdout or out.stderr).strip().splitlines() or ["UNKNOWN"])[0]
        return first.strip() or "UNKNOWN"

    @abstractmethod
    def write_driver_config(self, dest: Path, server_entries: dict[str, dict]) -> Path:
        """Write the driver-native MCP registration file pointing at the given
        stdio commands ({name: {command, args, env?}}); return its path."""

    @abstractmethod
    def build_turn(self, ctx: TurnContext) -> TurnSpec:
        """The full argv (and answer channel) for one turn."""

    @abstractmethod
    def attribution_record(self, argv: list[str]) -> dict:
        """Assert -- from the argv actually about to execute, never from intent --
        that the non-server channels are closed; return the record of what was
        verified, in this driver's own vocabulary. Raises DriverAttributionError."""
