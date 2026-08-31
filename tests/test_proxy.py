"""The trace-recording proxy: pinned record fields, surface enforcement, secret handling.

The client here plays the driver's role; the distractor server plays the SUT. Every
byte crosses two real process boundaries, so what these tests see is what a real
driver would.
"""
from __future__ import annotations

import json
import sys

import pytest

from mcp_e2e_harness.checks import PINNED_RECORD_FIELDS
from mcp_e2e_harness.mcp_client import MCPClientError, StdioMCPClient


def proxy_client(tmp_path, allow_tools: str | None = None,
                 server_env: dict | None = None) -> StdioMCPClient:
    server_config = tmp_path / "server-config.json"
    server_config.write_text(json.dumps({
        "command": sys.executable,
        "args": ["-m", "mcp_e2e_harness.distractor", "--procedure", "neutral-file-triage@1"],
        "env": server_env or {},
    }))
    args = ["-m", "mcp_e2e_harness.proxy",
            "--server-config", str(server_config),
            "--trace-file", str(tmp_path / "trace.jsonl"),
            "--tools-file", str(tmp_path / "available-tools.json"),
            "--meta-file", str(tmp_path / "proxy-meta.json")]
    if allow_tools is not None:
        args += ["--allow-tools", allow_tools]
    return StdioMCPClient(sys.executable, args, timeout=30)


def read_trace(tmp_path) -> list[dict]:
    path = tmp_path / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_trace_records_carry_every_pinned_field(tmp_path):
    with proxy_client(tmp_path) as c:
        c.list_tools()
        result = c.call_tool("list_unfiled_notes", {})
        assert result["is_error"] is False
        c.call_tool("read_note", {"note_id": "n03"})
    records = read_trace(tmp_path)
    assert [r["tool"] for r in records] == ["list_unfiled_notes", "read_note"]
    for i, record in enumerate(records):
        for field in PINNED_RECORD_FIELDS:
            assert field in record, f"pinned field {field} missing"
        assert record["index"] == i
        assert record["response_bytes"] == len(json.dumps(record["response"]).encode())
        assert record["duration_ms"] >= 0
    assert records[1]["args"] == {"note_id": "n03"}
    # The response is the tool result verbatim -- parsed structure, not a paraphrase.
    assert records[0]["response"]["content"][0]["type"] == "text"


def test_tool_error_is_flagged_in_the_trace(tmp_path):
    with proxy_client(tmp_path) as c:
        c.call_tool("read_note", {"note_id": "ghost"})
    records = read_trace(tmp_path)
    assert records[0]["is_error"] is True


def test_available_tool_surface_is_recorded(tmp_path):
    with proxy_client(tmp_path) as c:
        tools = c.list_tools()
        assert len(tools) == 3
    surface = json.loads((tmp_path / "available-tools.json").read_text())
    assert surface["advertised"] == ["list_unfiled_notes", "read_note", "file_note"]
    assert surface["exposed"] == surface["advertised"]


def test_list_valued_surface_is_enforced_not_assumed(tmp_path):
    with proxy_client(tmp_path, allow_tools="read_note") as c:
        tools = c.list_tools()
        # tools/list is filtered: the consumer never sees off-surface tools.
        assert [t["name"] for t in tools] == ["read_note"]
        on_surface = c.call_tool("read_note", {"note_id": "n01"})
        assert on_surface["is_error"] is False
        blocked = c.call_tool("list_unfiled_notes", {})
        assert blocked["is_error"] is True
        assert "not on this cell's tool surface" in json.dumps(blocked["response"])
    surface = json.loads((tmp_path / "available-tools.json").read_text())
    assert surface["advertised"] == ["list_unfiled_notes", "read_note", "file_note"]
    assert surface["exposed"] == ["read_note"]
    # The blocked call never reached the server: no trace record, but the attempt is
    # evidence and lands in the proxy meta.
    records = read_trace(tmp_path)
    assert [r["tool"] for r in records] == ["read_note"]
    meta = json.loads((tmp_path / "proxy-meta.json").read_text())
    assert [b["tool"] for b in meta["blocked_calls"]] == ["list_unfiled_notes"]


def test_proxy_meta_reports_server_exit(tmp_path):
    with proxy_client(tmp_path) as c:
        c.call_tool("list_unfiled_notes", {})
    meta = json.loads((tmp_path / "proxy-meta.json").read_text())
    assert meta["server_exit"] == 0
    assert meta["malformed_driver_lines"] == 0
    assert meta["unanswered_calls"] == []


def test_secret_placeholder_resolves_from_proxy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PROXY_TEST_SECRET", "resolved-secret-value")
    with proxy_client(tmp_path, server_env={"INJECTED": {"$secret": "PROXY_TEST_SECRET"}}) as c:
        c.call_tool("list_unfiled_notes", {})
    # The placeholder stays a placeholder in the artifact; no resolved value on disk.
    assert "resolved-secret-value" not in (tmp_path / "server-config.json").read_text()
    assert "resolved-secret-value" not in (tmp_path / "trace.jsonl").read_text()


def test_missing_secret_kills_the_proxy_loudly(tmp_path):
    client = proxy_client(tmp_path, server_env={"KEY": {"$secret": "DEFINITELY_UNSET_VAR"}})
    with pytest.raises(MCPClientError, match="DEFINITELY_UNSET_VAR"):
        client.start()
    client.close()
