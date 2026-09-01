"""The API-boundary tool-surface capture (Q2's preferred instrument).

Every test drives the recorder over real HTTP: client -> recorder -> fake upstream.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from mcp_e2e_harness.api_capture import ApiSurfaceRecorder, summarize


def post(base: str, path: str, payload: dict, headers: dict | None = None):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
    return urllib.request.urlopen(request, timeout=30)


def test_records_tool_names_and_forwards_verbatim(tmp_path, fake_api_upstream):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        response = post(base, "/v1/messages", {
            "model": "m-1",
            "tools": [{"name": "mcp__s__t", "input_schema": {}}, {"name": "TodoWrite"}],
            "messages": [{"role": "user", "content": "VERY-PRIVATE-PROMPT"}],
        }, headers={"x-api-key": "sk-super-secret-key"})
        assert response.status == 200
        assert b"fake-upstream" in response.read()
    finally:
        recorder.stop()

    lines = [json.loads(line) for line in (tmp_path / "cap.jsonl").read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["tool_names"] == ["mcp__s__t", "TodoWrite"]
    assert lines[0]["model"] == "m-1"
    assert lines[0]["path"] == "/v1/messages"
    # Only names ever land in the artifact: no credentials, no message content,
    # no tool schemas.
    raw = (tmp_path / "cap.jsonl").read_text()
    assert "sk-super-secret-key" not in raw
    assert "VERY-PRIVATE-PROMPT" not in raw
    assert "input_schema" not in raw


def test_type_named_hosted_tools_are_recorded(tmp_path, fake_api_upstream):
    # Responses-API hosted tools (web_search and kin) carry a "type" and no "name";
    # recording only names would blind the disallowed check to exactly these.
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        post(base, "/v1/responses", {
            "model": "gpt-x",
            "tools": [{"type": "web_search"}, {"type": "function", "name": "shell"}],
            "input": [],
        })
    finally:
        recorder.stop()
    record = json.loads((tmp_path / "cap.jsonl").read_text().splitlines()[0])
    assert record["tool_names"] == ["web_search", "shell"]
    digest = summarize(tmp_path / "cap.jsonl", ("web_search", "web_search_preview"))
    assert digest["disallowed_builtins_on_wire"] == ["web_search"]


def test_tool_less_requests_are_recorded_with_null_names(tmp_path, fake_api_upstream):
    # A routed-but-tool-less request must stay distinguishable from "never routed"
    # (measured: codex sends tool-less requests as part of a working turn).
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        assert post(base, "/v1/messages", {"model": "m-1", "messages": []}).status == 200
        assert urllib.request.urlopen(base + "/health", timeout=30).status == 200
    finally:
        recorder.stop()
    lines = [json.loads(line) for line in (tmp_path / "cap.jsonl").read_text().splitlines()]
    assert len(lines) == 1  # the GET had no body; the POST is recorded tool-less
    assert lines[0]["tool_names"] is None
    assert lines[0]["body_keys"] == ["messages", "model"]
    digest = summarize(tmp_path / "cap.jsonl", ("Bash",))
    assert digest["requests_recorded"] == 1
    assert digest["requests_with_tools"] == 0
    assert recorder.unrecorded_requests == 0


def test_code_mode_tool_roster_is_read_from_turn_metadata(tmp_path, fake_api_upstream):
    # codex-family models send no tools array; the roster travels in
    # client_metadata["x-codex-turn-metadata"].code_mode_tool_names (measured by
    # full-body probe, 2026-09-01). Both the string- and dict-valued forms parse.
    turn_meta = {"turn_id": "t1", "code_mode_tool_names": {
        "exec_command": {"name": "exec_command", "namespace": None},
        "mcp__sut__list_notes": {"name": "list_notes", "namespace": "mcp__sut"},
    }}
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        post(base, "/v1/responses", {"model": "gpt-5.6-luna", "input": [],
                                     "client_metadata": {"x-codex-turn-metadata": turn_meta}})
        post(base, "/v1/responses", {"model": "gpt-5.6-luna", "input": [],
                                     "client_metadata": {
                                         "x-codex-turn-metadata": json.dumps(turn_meta)}})
    finally:
        recorder.stop()
    lines = [json.loads(line) for line in (tmp_path / "cap.jsonl").read_text().splitlines()]
    for record in lines:
        assert record["tool_names"] == ["exec_command", "mcp__sut__list_notes"]
        assert record["tool_source"] == "code_mode_tool_names"
    digest = summarize(tmp_path / "cap.jsonl", ("web_search",))
    assert digest["requests_with_tools"] == 2
    assert digest["disallowed_builtins_on_wire"] == []


def test_unparseable_body_is_counted_with_its_encoding(tmp_path, fake_api_upstream):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        request = urllib.request.Request(base + "/v1/messages", data=b"\x1f\x8b not-json",
                                         headers={"Content-Type": "application/json",
                                                  "Content-Encoding": "gzip"},
                                         method="POST")
        urllib.request.urlopen(request, timeout=30)
    finally:
        recorder.stop()
    assert recorder.unrecorded_requests == 1
    assert recorder.unrecorded_encodings == {"gzip"}


def test_dead_upstream_becomes_502_and_is_counted(tmp_path):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", "http://127.0.0.1:9")
    base = recorder.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/v1/messages", {"model": "m", "tools": [], "messages": []})
        assert exc.value.code == 502
    finally:
        recorder.stop()
    assert recorder.forward_errors == 1


def test_upstream_must_be_http(tmp_path):
    with pytest.raises(ValueError, match="http"):
        ApiSurfaceRecorder(tmp_path / "cap.jsonl", "not-a-url")


def test_summarize_digest_and_disallowed_detection(tmp_path):
    path = tmp_path / "cap.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "tool_names": ["mcp__s__a", "TodoWrite"]}) + "\n"
        + json.dumps({"seq": 2, "tool_names": ["mcp__s__a", "Bash"]}) + "\n")
    digest = summarize(path, ("Bash", "WebFetch"))
    assert digest["requests_with_tools"] == 2
    assert digest["tool_names_union"] == ["mcp__s__a", "TodoWrite", "Bash"]
    assert digest["disallowed_builtins_on_wire"] == ["Bash"]
    assert summarize(tmp_path / "missing.jsonl", ("Bash",)) is None
