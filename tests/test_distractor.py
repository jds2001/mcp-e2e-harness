"""The distractor MCP server, driven by the minimal MCP client over real stdio."""
from __future__ import annotations

import json
import sys

from mcp_e2e_harness.mcp_client import StdioMCPClient


def client(tmp_path, state: bool = True) -> StdioMCPClient:
    args = ["-m", "mcp_e2e_harness.distractor", "--procedure", "neutral-file-triage@2"]
    if state:
        args += ["--state-file", str(tmp_path / "state.json")]
    return StdioMCPClient(sys.executable, args, timeout=30)


def payload_of(record: dict) -> dict:
    return json.loads(record["response"]["content"][0]["text"])


def test_initialize_lists_and_calls(tmp_path):
    with client(tmp_path) as c:
        assert c.server_info["name"] == "shared_notes"
        tools = c.list_tools()
        assert {t["name"] for t in tools} == {"list_unfiled_notes", "read_note", "file_note"}

        listing = c.call_tool("list_unfiled_notes", {})
        assert listing["is_error"] is False
        for field in ("index",):  # index is proxy-assigned; the client shape has the rest
            assert field not in listing
        for field in ("tool", "args", "response", "is_error", "started_at",
                      "duration_ms", "response_bytes"):
            assert field in listing
        assert payload_of(listing)["count"] == 12

        note = payload_of(c.call_tool("read_note", {"note_id": "n01"}))
        assert note["title"] == "Printer toner"

        filed = payload_of(c.call_tool("file_note", {"note_id": "n01", "folder": "facilities"}))
        assert filed == {"filed": "n01", "folder": "facilities", "remaining": 11}


def test_errors_are_flagged(tmp_path):
    with client(tmp_path) as c:
        bad = c.call_tool("read_note", {"note_id": "ghost"})
        assert bad["is_error"] is True
        bad_folder = c.call_tool("file_note", {"note_id": "n01", "folder": "attic"})
        assert bad_folder["is_error"] is True


def test_filed_state_persists_across_restarts(tmp_path):
    with client(tmp_path) as c:
        payload_of(c.call_tool("file_note", {"note_id": "n02", "folder": "social"}))
    with client(tmp_path) as c:
        listing = payload_of(c.call_tool("list_unfiled_notes", {}))
        assert listing["count"] == 11
        assert "n02" not in [n["id"] for n in listing["unfiled"]]
