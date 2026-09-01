"""The claude-code driver: Claude Code headless (`claude -p`).

Isolation invariants, inherited from the ancestor harness where each was earned:

* ``--strict-mcp-config`` shuts out whatever MCP servers the operator happens to have
  registered; the surface is exactly the harness-written config.
* Every builtin that could answer untraced (web, filesystem, shell, subagents) is in
  ``--disallowed-tools``: a claim the model reached by fetching a web page or reading a
  file is invisible to the instrument.
* The prompt goes in on stdin, never argv (keeps it out of the process table, and
  claude's variadic flags would swallow a trailing positional).
* The attribution record is ASSERTED from the argv about to be executed, never from
  intent -- a record produced any other way could drift and report a closed channel
  that was open.

Environment inheritance (measured on the ancestor): claude passes its own environment
to the stdio MCP children it spawns, so ``$secret`` values exported to the harness
reach the proxy -- and through it the server -- without appearing in any artifact.

Knobs are recorded verbatim in the meta; this driver passes none of them to the CLI.
A knob that needs an invocation-level flag is a driver change, made here, in claude's
own vocabulary.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Driver, DriverAttributionError, TurnContext, TurnSpec

DISALLOWED_BUILTINS = (
    "WebSearch", "WebFetch", "Bash", "Read", "Write", "Edit",
    "Glob", "Grep", "Task", "NotebookEdit",
)
# The subset whose absence the attribution record must positively verify: the channels
# that could produce an untraced answer (web) or leak the surroundings (fs/shell).
ATTRIBUTION_CRITICAL = ("WebSearch", "WebFetch", "Bash", "Read")


class ClaudeCodeDriver(Driver):
    id = "claude-code"
    executable = "claude"
    disallowed_builtins = DISALLOWED_BUILTINS
    probe_model = "claude-haiku-4-5-20251001"
    api_base_env = "ANTHROPIC_BASE_URL"
    api_default_upstream = "https://api.anthropic.com"

    def write_driver_config(self, dest: Path, server_entries: dict[str, dict]) -> Path:
        config = {"mcpServers": server_entries}
        path = dest / "mcp-config.json"
        path.write_text(json.dumps(config, indent=2) + "\n")
        return path

    def _allowed_tools(self, ctx: TurnContext) -> str:
        if ctx.tool_surface == "full":
            allowed = [f"mcp__{ctx.server_name}"]
        else:
            allowed = [f"mcp__{ctx.server_name}__{tool}" for tool in ctx.tool_surface]
        allowed += [f"mcp__{name}" for name in ctx.extra_server_names]
        return ",".join(allowed)

    def build_turn(self, ctx: TurnContext) -> TurnSpec:
        argv = [
            self.executable, "-p", "--model", ctx.model,
            "--strict-mcp-config",
            "--mcp-config", str(ctx.mcp_config_path),
            # SURFACE REMOVAL, not just permission denial. Measured (Q2 probe,
            # 2026-08-31, claude 2.1.221): with only --disallowed-tools, the consumer
            # still enumerated Read/Edit/Write/Glob/Grep/Bash in its own tool surface
            # -- disallow denies invocation but leaves the tools visible. --tools ""
            # empties the built-in set entirely; MCP tools arrive via --mcp-config and
            # are unaffected (the probe's positive control verifies that every run).
            "--tools", "",
            "--permission-mode", "acceptEdits",
            "--allowed-tools", self._allowed_tools(ctx),
            # Belt over the surface removal: even a tool that somehow re-enters the
            # set (a CLI upgrade, a deferred loader) is denied at invocation.
            "--disallowed-tools", ",".join(DISALLOWED_BUILTINS),
        ]
        mode, session_id = ctx.session
        if mode == "open":
            argv += ["--session-id", session_id]
        elif mode == "resume":
            argv += ["--resume", session_id]
        # Route API traffic through the harness recorder (S7 wire capture); claude
        # honors ANTHROPIC_BASE_URL (measured live 2026-08-31, zero forward errors).
        env_overrides = ({"ANTHROPIC_BASE_URL": ctx.api_base_url}
                         if ctx.api_base_url else {})
        return TurnSpec(argv=argv, stdin_text=ctx.prompt, answer_from="stdout",
                        env_overrides=env_overrides)

    def attribution_record(self, turn: TurnSpec) -> dict:
        argv = turn.argv
        def missing(what: str) -> DriverAttributionError:
            return DriverAttributionError(
                f"the claude-code argv lacks {what}. The attribution record must be asserted "
                "from the executed command, and this command does not close the channel it "
                "claims to. Harness bug; cell refused as instrument-broken.")

        if "--strict-mcp-config" not in argv:
            raise missing("--strict-mcp-config")
        try:
            if argv[argv.index("--tools") + 1] != "":
                raise missing('--tools "" (an empty built-in set; a non-empty value leaves '
                              "builtins on the consumer's surface)")
        except (ValueError, IndexError):
            raise missing('--tools ""') from None
        try:
            disallowed = argv[argv.index("--disallowed-tools") + 1].split(",")
            allowed = argv[argv.index("--allowed-tools") + 1]
        except (ValueError, IndexError):
            raise missing("--disallowed-tools/--allowed-tools") from None
        for tool in ATTRIBUTION_CRITICAL:
            if tool not in disallowed:
                raise missing(f"{tool} in --disallowed-tools")
        return {
            "strict_mcp_config": True,
            "builtin_tool_set": "(empty via --tools \"\")",
            "allowed_tools": allowed,
            "disallowed_tools": disallowed,
        }
