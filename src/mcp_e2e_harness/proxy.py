"""Trace-recording stdio proxy: sits between the driver and the MCP server under test.

The harness -- not the server -- owns trace capture, because the pinned trace-record
fields are guaranteed "on every record regardless of driver" (documentation/
30-checks.md) and the harness may contain no knowledge of any particular server
(documentation/10-harness.md). The ancestor implementation leaned on the server's own
trace switch; a generic harness cannot, so the driver is pointed at this proxy, which
spawns the real server and relays newline-delimited JSON-RPC in both directions,
recording every ``tools/call`` round-trip with the pinned fields:

    index, tool, args, response, is_error, started_at, duration_ms, response_bytes

It also records the ``tools/list`` responses -- the recorded available-tool surface;
disabling a tool is a claim, the tool listing is the evidence -- and, for list-valued
``tool_surface`` cells, ENFORCES the surface: tools off the list are filtered out of
``tools/list`` and a ``tools/call`` for one is answered with a JSON-RPC error without
ever reaching the server (recorded in the proxy meta as a blocked call, not in the
trace -- the trace is the record of what reached the server). That is what makes
"trace scope equals tool surface" true by construction rather than by assumption.

Relaying never depends on parsing: a line that fails to parse is forwarded verbatim
and counted in the meta. Secrets in the server registration arrive as
``{"$secret": "VAR"}`` placeholders and are resolved here, from this process's own
environment, at spawn time -- they exist in no artifact.

Invoked by the harness-written driver config, never by hand:

    python -m mcp_e2e_harness.proxy --server-config X.json --trace-file t.jsonl \
        --tools-file tools.json --meta-file meta.json [--allow-tools a,b,c]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .secrets import MissingSecretError, resolve_env_map


class _State:
    def __init__(self, trace_file: Path, tools_file: Path, meta_file: Path,
                 allow_tools: list[str] | None):
        self.trace_file = trace_file
        self.tools_file = tools_file
        self.meta_file = meta_file
        self.allow_tools = allow_tools
        self.lock = threading.Lock()
        self.meta_lock = threading.Lock()
        self.pending_calls: dict[object, dict] = {}   # request id -> {tool, args, started...}
        self.pending_lists: set = set()               # request ids of tools/list
        self.server_spawn: dict | None = None
        self.index = 0
        self.blocked: list[dict] = []
        self.malformed_driver_lines = 0
        self.malformed_server_lines = 0

    def write_meta(self, server_exit: int | None, shutdown: str) -> None:
        """Written at startup, on SIGTERM, and at clean exit -- never only at exit.

        Measured (2026-08-31 live run): claude terminates its MCP children with
        SIGTERM when the turn ends, so an exit-only meta write produced no meta file
        at all while the incrementally-flushed trace survived. Blocked-call evidence
        must not depend on a clean shutdown the driver never promises.
        """
        with self.meta_lock:
            self.meta_file.write_text(json.dumps({
                **({"server_spawn": self.server_spawn} if self.server_spawn else {}),
                "blocked_calls": self.blocked,
                "malformed_driver_lines": self.malformed_driver_lines,
                "malformed_server_lines": self.malformed_server_lines,
                "unanswered_calls": [p["tool"] for p in self.pending_calls.values()],
                "server_exit": server_exit,
                "shutdown": shutdown,
            }, indent=2) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_trace(state: _State, pending: dict, response_body, is_error: bool) -> None:
    record = {
        "index": state.index,
        "tool": pending["tool"],
        "args": pending["args"],
        "response": response_body,
        "is_error": is_error,
        "started_at": pending["started_at"],
        "duration_ms": round((time.monotonic() - pending["t0"]) * 1000, 3),
        "response_bytes": len(json.dumps(response_body).encode()),
    }
    state.index += 1
    with open(state.trace_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()


def _pump_driver_to_server(state: _State, driver_in, server_in, driver_out, out_lock) -> None:
    """Relay driver -> server; register tools/call and tools/list; enforce the surface."""
    try:
        for raw in driver_in:
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            forward = True
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = None
                state.malformed_driver_lines += 1
            if isinstance(msg, dict) and "method" in msg and "id" in msg:
                method = msg["method"]
                if method == "tools/call":
                    params = msg.get("params") or {}
                    tool = params.get("name")
                    if state.allow_tools is not None and tool not in state.allow_tools:
                        # Off-surface call: never reaches the server, so never a trace
                        # record; the attempt is evidence and lands in the proxy meta.
                        forward = False
                        state.blocked.append({"tool": tool, "at": _now_iso()})
                        # Re-persist immediately: this is the evidence an isolation
                        # cell's scoring leans on, and the driver may kill us later.
                        state.write_meta(None, "running")
                        refusal = json.dumps({
                            "jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32602,
                                      "message": f"tool {tool!r} is not on this cell's tool surface"},
                        }).encode()
                        with out_lock:
                            driver_out.write(refusal + b"\n")
                            driver_out.flush()
                    else:
                        with state.lock:
                            state.pending_calls[msg["id"]] = {
                                "tool": tool, "args": params.get("arguments"),
                                "started_at": _now_iso(), "t0": time.monotonic(),
                            }
                elif method == "tools/list":
                    with state.lock:
                        state.pending_lists.add(msg["id"])
            if forward:
                server_in.write(line + b"\n")
                server_in.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            server_in.close()


def _pump_server_to_driver(state: _State, server_out, driver_out, out_lock) -> None:
    """Relay server -> driver; record tools/call responses and the advertised surface."""
    try:
        for raw in server_out:
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = None
                state.malformed_server_lines += 1
            if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                msg_id = msg["id"]
                with state.lock:
                    pending = state.pending_calls.pop(msg_id, None)
                    is_list = msg_id in state.pending_lists
                    state.pending_lists.discard(msg_id)
                if pending is not None:
                    body = msg.get("result", msg.get("error"))
                    is_error = ("error" in msg) or (
                        isinstance(msg.get("result"), dict) and msg["result"].get("isError") is True)
                    _record_trace(state, pending, body, is_error)
                if is_list and isinstance(msg.get("result"), dict):
                    advertised = [t.get("name") for t in msg["result"].get("tools", [])
                                  if isinstance(t, dict)]
                    if state.allow_tools is not None:
                        kept = [t for t in msg["result"].get("tools", [])
                                if isinstance(t, dict) and t.get("name") in state.allow_tools]
                        msg["result"]["tools"] = kept
                        line = json.dumps(msg).encode()
                    exposed = [t.get("name") for t in msg["result"].get("tools", [])
                               if isinstance(t, dict)]
                    state.tools_file.write_text(json.dumps(
                        {"advertised": advertised, "exposed": exposed}, indent=2) + "\n")
            with out_lock:
                driver_out.write(line + b"\n")
                driver_out.flush()
    except (BrokenPipeError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", required=True)
    parser.add_argument("--trace-file", required=True)
    parser.add_argument("--tools-file", required=True)
    parser.add_argument("--meta-file", required=True)
    parser.add_argument("--record-server-spawn", action="store_true")
    parser.add_argument("--allow-tools", default=None,
                        help="comma-separated tool allowlist for list-valued tool_surface cells")
    parser.add_argument("--secrets-file", default=None,
                        help="0600 KEY=VALUE file to overlay on this process's environment for "
                             "$secret resolution -- for drivers (codex) that sanitize the env of "
                             "MCP servers they spawn, so inheritance delivers nothing here. "
                             "Written and deleted by the harness; never an operator input.")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.server_config).read_text())
    env = dict(os.environ)
    environ: dict[str, str] = dict(os.environ)
    if args.secrets_file:
        # Refuse a file readable by group/other: a shared-readable credential file is
        # a disclosure, not a configuration; failing loudly at spawn beats trusting
        # every caller to have set the mode.
        try:
            mode = os.stat(args.secrets_file).st_mode
        except OSError as exc:
            sys.stderr.write(f"mcp_e2e_harness.proxy: cannot stat secrets file: {exc}\n")
            return 3
        if mode & 0o077:
            sys.stderr.write(
                f"mcp_e2e_harness.proxy: secrets file {args.secrets_file} is readable by "
                f"group/other (mode {oct(mode & 0o777)}); refusing to start. chmod 600 it.\n")
            return 3
        for line in Path(args.secrets_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if key and value:
                    environ[key] = value
    try:
        resolved, _ = resolve_env_map(config.get("env") or {}, environ, where="server env")
    except MissingSecretError as exc:
        sys.stderr.write(f"mcp_e2e_harness.proxy: {exc}\n")
        return 3
    env.update(resolved)

    allow = args.allow_tools.split(",") if args.allow_tools is not None else None
    state = _State(Path(args.trace_file), Path(args.tools_file), Path(args.meta_file), allow)

    proc = subprocess.Popen(
        [config["command"], *config.get("args", [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr.fileno(),
        env=env, cwd=config.get("cwd") or None,
    )
    if args.record_server_spawn:
        state.server_spawn = {"pid": proc.pid, "started_at": datetime.now(timezone.utc).isoformat()}
    state.write_meta(None, "running")

    def _on_signal(signum, frame):  # noqa: ARG001
        # The driver owns this process's lifetime and ends it with SIGTERM, not EOF;
        # persist what we know, pass the signal on to the server, and go.
        state.write_meta(None, f"signal-{signum}")
        with contextlib.suppress(OSError):
            proc.terminate()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    driver_in = sys.stdin.buffer
    driver_out = sys.stdout.buffer
    out_lock = threading.Lock()

    t_up = threading.Thread(target=_pump_driver_to_server,
                            args=(state, driver_in, proc.stdin, driver_out, out_lock), daemon=True)
    t_down = threading.Thread(target=_pump_server_to_driver,
                              args=(state, proc.stdout, driver_out, out_lock), daemon=True)
    t_up.start()
    t_down.start()
    t_up.join()
    t_down.join()
    exit_code = proc.wait()
    state.write_meta(exit_code, "clean")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
