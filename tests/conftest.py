"""Shared test fixtures.

The server under test in integration tests is the harness's own distractor MCP server
(``mcp_e2e_harness.distractor``): a real stdio MCP server with zero external
dependencies. Using it as the SUT keeps the tests hermetic -- no network, no
credentials -- while exercising the full JSON-RPC path through the proxy.

The consumer is ``tests/fake_consumer.py`` run through ``FakeDriver``: a real
subprocess that reads the harness-written MCP config, spawns the configured server
commands (i.e. the proxy), and makes real tool calls -- everything the claude-code
driver does except the model.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
FAKE_CONSUMER = TESTS_DIR / "fake_consumer.py"

sys.path.insert(0, str(REPO / "src"))

from mcp_e2e_harness.drivers.base import Driver, TurnContext, TurnSpec  # noqa: E402


class FakeDriver(Driver):
    id = "fake"
    executable = sys.executable
    disallowed_builtins = ("FakeWeb",)
    probe_model = "fake-model-1"

    def write_driver_config(self, dest: Path, server_entries: dict[str, dict]) -> Path:
        path = dest / "mcp-config.json"
        path.write_text(json.dumps({"mcpServers": server_entries}, indent=2) + "\n")
        return path

    def build_turn(self, ctx: TurnContext) -> TurnSpec:
        argv = [sys.executable, str(FAKE_CONSUMER), str(ctx.mcp_config_path)]
        env_overrides = ({"FAKE_API_BASE": ctx.api_base_url} if ctx.api_base_url else {})
        return TurnSpec(argv=argv, stdin_text=ctx.prompt, env_overrides=env_overrides)

    def attribution_record(self, turn: TurnSpec) -> dict:
        return {"fake": True}


BASE_MANIFEST: dict = {
    "_about": "hermetic test suite: the SUT is the harness's own distractor server",
    "suite": {"name": "test-suite", "spec": "tests/conftest.py", "manifest_version": "1"},
    "server": {
        "name": "notes_sut",
        "transport": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "mcp_e2e_harness.distractor", "--procedure", "neutral-file-triage@2"],
        },
    },
    "cells": {
        "basic": {
            "driver": "fake",
            "model": "fake-model-1",
            "knobs": {"thinking": "none"},
            "role": "floor",
            "context": "fresh",
            "tool_surface": "full",
            "merge_gating": True,
            "groups": ["A"],
        },
    },
    "prompts": [
        {
            "id": "A1",
            "group": "A",
            "title": "list the notes",
            "prompt": "Please list the unfiled notes.",
            "sourcing": "measured",
            "grounding": "the distractor procedure defines 12 notes; see crowding.py",
            "pass": "lists notes",
            "fail": "fabricates notes",
        },
    ],
}


def manifest_data(**top_level_overrides) -> dict:
    data = copy.deepcopy(BASE_MANIFEST)
    data.update(top_level_overrides)
    return data


def write_manifest(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data, indent=2))
    return path


@pytest.fixture()
def fake_drivers() -> dict[str, Driver]:
    return {"fake": FakeDriver()}


@pytest.fixture()
def fake_api_upstream():
    """A stand-in model API for capture tests: accepts anything, returns {"ok": true}."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: ARG002
            pass

        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            payload = b'{"ok": true, "from": "fake-upstream"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = _reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
