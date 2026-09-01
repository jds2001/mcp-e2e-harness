"""Driver interface.

The driver is part of the instrument: two cells that differ in driver also differ in
system prompt, tool-call formatting, and client-side behavior the trace cannot see, so
knobs are recorded in the driver's native vocabulary, verbatim, never translated
across vendors (documentation/10-harness.md, run mechanics). What each driver must
establish before it may host attribution cells is per-driver contract, recorded in
documentation/50-drivers.md.
"""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class DriverAttributionError(RuntimeError):
    """The turn about to run does not verifiably close the channels it claims to.

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
    # Extra environment the runner applies on top of its own when executing this turn
    # (e.g. CODEX_HOME for an isolated codex home, ANTHROPIC_BASE_URL for the claude
    # recorder). Recorded in meta as executed material; must never carry a secret
    # value -- secrets travel by inheritance or the spawn-time secrets file only.
    env_overrides: dict[str, str] = field(default_factory=dict)


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
    # The harness API-surface recorder's local URL for this invocation, when capture
    # is active (S7). The driver routes its outbound model-API traffic through it --
    # by env var, config, or argv, in its own idiom. None on dry runs and for
    # capture-less drivers.
    api_base_url: str | None = None


class Driver(ABC):
    id: str
    executable: str
    # Builtin tool names this driver disallows; checked against the calibration
    # probe's enumeration and against every wire-captured tools array (Q2).
    disallowed_builtins: tuple[str, ...] = ()
    # Default model for the builtin-surface probe when none is given, in the driver's
    # own vocabulary. Empty means the driver cannot be probed.
    probe_model: str = ""
    # Default model for the egress canary (50-drivers.md, codex residual): a driver
    # whose consumer retains shell-shaped tools must demonstrate, under harness
    # instruments, that a network fetch from that shell fails honestly. Empty means
    # the canary does not apply (no shell-shaped channel survives the driver's
    # surface removal) or the driver cannot run it.
    egress_probe_model: str = ""
    # Harness env var that overrides this driver's API upstream (e.g. an operator's
    # gateway); consulted before api_default_upstream.
    api_base_env: str = ""
    # Where the driver's API traffic goes when nothing overrides it. Non-empty means
    # the driver supports API-boundary capture (Q2's preferred instrument): every
    # invocation routes through the harness recorder and is verified on the wire.
    # Empty means capture is unsupported and merge-gating cells fall back to the
    # calibration probe.
    api_default_upstream: str = ""
    # Markers of the driver's own web-tool activity in its event streams (stderr, and
    # stdout when the answer travels by file). Any hit is an instrument breach: the
    # row is not tool-attributable and the cell is BROKEN (50-drivers.md, codex #5).
    web_event_markers: tuple[str, ...] = ()
    # True when the driver sanitizes the environment of MCP servers it spawns, so
    # {"$secret": ...} values cannot reach the server by inheritance; the runner then
    # provides a spawn-time secrets file to the proxy (process-env only, never an
    # artifact -- 50-drivers.md, codex #6).
    sanitizes_mcp_env: bool = False
    # Env vars that must be set in the harness environment before this driver can run
    # at all (e.g. the custom provider's env_key). Checked at preflight, loudly.
    required_env: tuple[str, ...] = ()
    # Whether the driver supports multi-turn sessions (crowding pre-turns). A crowded
    # cell on a driver without sessions is refused at preflight.
    supports_sessions: bool = True

    @property
    def supports_api_capture(self) -> bool:
        return bool(self.api_default_upstream)

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
        """Write the driver-native MCP registration pointing at the given stdio
        commands ({name: {command, args, env?}}); return the config file's path."""

    @abstractmethod
    def build_turn(self, ctx: TurnContext) -> TurnSpec:
        """The full argv, env overrides, and answer channel for one turn."""

    @abstractmethod
    def attribution_record(self, turn: TurnSpec) -> dict:
        """Assert -- from the turn actually about to execute (argv and env overrides),
        never from intent -- that the non-server channels are closed; return the
        record of what was verified, in this driver's own vocabulary. Raises
        DriverAttributionError."""
