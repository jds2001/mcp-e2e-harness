"""Minimal stdio MCP client, used for a cell's ``setup`` actions.

Setup actions are direct calls the harness makes against the server before the prompt
-- never via a model turn -- and exactly what was done is recorded in the cell's meta
(documentation/10-harness.md). The server process spawned here is separate from the
one the prompt's driver talks to (S5: fresh server process per cell invocation), so a
setup action's effect must persist through state the cell declares explicitly --
the ``env`` both processes share is the only channel. That is the generic form of the
ancestor's cache pre-warm, which shared a declared cache directory.

Speaks newline-delimited JSON-RPC 2.0: initialize, notifications/initialized,
tools/list, tools/call. Server-initiated requests are answered with a method-not-found
error; notifications are ignored. Call results are returned in the same pinned-field
shape as trace records so setup artifacts read like traces.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone

from . import __version__

PROTOCOL_VERSION = "2025-06-18"


class MCPClientError(RuntimeError):
    pass


class StdioMCPClient:
    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None,
                 timeout: float = 60.0):
        self._cmd = [command, *(args or [])]
        self._env = env
        self._cwd = cwd
        self._timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._next_id = 0

    def __enter__(self) -> StdioMCPClient:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=self._env, cwd=self._cwd)
        except OSError as exc:
            raise MCPClientError(f"cannot spawn server {self._cmd[0]!r}: {exc}") from None
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-e2e-harness-setup", "version": __version__},
        })
        self.server_info = result.get("result", {}).get("serverInfo")
        self._notify("notifications/initialized")

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._queue.put(msg)
        self._queue.put(None)  # EOF sentinel

    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in self._proc.stderr:
            tail = getattr(self, "stderr_tail", None)
            if tail is not None:
                tail.append(raw.decode(errors="replace"))
                del tail[:-50]

    def _send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        try:
            self._proc.stdin.write(json.dumps(msg).encode() + b"\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPClientError(f"server closed its stdin: {exc}") from None

    def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        req_id = self._next_id
        msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPClientError(f"timeout waiting for response to {method}")
            try:
                incoming = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise MCPClientError(f"timeout waiting for response to {method}") from None
            if incoming is None:
                raise MCPClientError(
                    f"server exited before responding to {method}; stderr tail: "
                    + "".join(getattr(self, "stderr_tail", []))[-800:])
            if "method" in incoming and "id" in incoming:
                # Server-initiated request; this client supports none of them.
                self._send({"jsonrpc": "2.0", "id": incoming["id"],
                            "error": {"code": -32601, "message": "method not supported"}})
                continue
            if "method" in incoming:
                continue  # notification from the server; ignore
            if incoming.get("id") == req_id:
                return incoming

    def list_tools(self) -> list[dict]:
        response = self._request("tools/list")
        if "error" in response:
            raise MCPClientError(f"tools/list failed: {response['error']}")
        return response.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool; return a record in the pinned trace-record field shape."""
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        body = response.get("result", response.get("error"))
        return {
            "tool": name,
            "args": arguments,
            "response": body,
            "is_error": ("error" in response) or (
                isinstance(response.get("result"), dict) and response["result"].get("isError") is True),
            "started_at": started,
            "duration_ms": round((time.monotonic() - t0) * 1000, 3),
            "response_bytes": len(json.dumps(body).encode()),
        }

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=10)
