"""The API-boundary tool-surface capture (Q2's preferred instrument).

Every test drives the recorder over real HTTP: client -> recorder -> fake upstream.
"""
from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

import brotli
import pytest
from conftest import FAKE_COUNT_TOKENS_VALUE

from mcp_e2e_harness.api_capture import RESPONSE_SCALAR_ALLOWLIST, ApiSurfaceRecorder, summarize

# The complete per-request key set (10-harness.md, recording contract). A key outside
# this set landing in a record is a contract breach, so tests pin it exactly.
REQUEST_RECORD_KEYS = {"seq", "at", "path", "model", "tool_names", "tool_source", "body_keys",
                       "body_bytes", "body_bytes_basis", "content_encoding"}
COUNT_TOKENS_KEYS = REQUEST_RECORD_KEYS | {"count_tokens", "count_tokens_note"}


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "cap.jsonl").read_text().splitlines()]


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


def test_unparseable_body_is_recorded_at_wire_size_with_its_encoding(tmp_path, fake_api_upstream):
    # Contract: "wire bytes with the encoding named otherwise". The request still gets
    # a line (context accounting needs every request's size) but is not *readable*:
    # no tool surface could be read off it, and the counters say so.
    wire = b"\x1f\x8b not-json"
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        request = urllib.request.Request(base + "/v1/messages", data=wire,
                                         headers={"Content-Type": "application/json",
                                                  "Content-Encoding": "gzip"},
                                         method="POST")
        urllib.request.urlopen(request, timeout=30)
    finally:
        recorder.stop()
    assert recorder.unrecorded_requests == 1
    assert recorder.unrecorded_encodings == {"gzip"}
    (record,) = records(tmp_path)
    assert set(record) == REQUEST_RECORD_KEYS
    assert record["body_bytes"] == len(wire)
    assert record["body_bytes_basis"] == "wire"
    assert record["content_encoding"] == "gzip"
    assert record["model"] is None and record["tool_names"] is None and record["body_keys"] is None
    digest = summarize(tmp_path / "cap.jsonl", ("Bash",))
    assert digest["requests_recorded"] == 1
    assert digest["requests_readable"] == 0


# ------------------------------------------------- context accounting (Q7 / E15)

def test_body_bytes_is_the_serialized_size_and_nothing_else_is_added(tmp_path, fake_api_upstream):
    payload = {"model": "m-1", "tools": [{"name": "mcp__s__t", "input_schema": {"x": "y" * 500}}],
               "messages": [{"role": "user", "content": "Z" * 10_000}]}
    serialized = json.dumps(payload).encode()
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        post(base, "/v1/messages", payload)
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert set(record) == REQUEST_RECORD_KEYS
    assert record["body_bytes"] == len(serialized)
    assert record["body_bytes_basis"] == "decompressed"
    assert record["content_encoding"] is None
    # Sizes are scalars: a 10k-char message adds bytes to the count and nothing to
    # the artifact.
    assert len((tmp_path / "cap.jsonl").read_text()) < 1000


def test_gzip_request_body_is_measured_decompressed(tmp_path, fake_api_upstream):
    payload = {"model": "m-1", "tools": [{"name": "mcp__s__t"}],
               "messages": [{"role": "user", "content": "A" * 5000}]}
    serialized = json.dumps(payload).encode()
    compressed = gzip.compress(serialized)
    assert len(compressed) < len(serialized)
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        request = urllib.request.Request(base + "/v1/messages", data=compressed,
                                         headers={"Content-Type": "application/json",
                                                  "Content-Encoding": "gzip"}, method="POST")
        urllib.request.urlopen(request, timeout=30)
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert record["body_bytes"] == len(serialized)
    assert record["body_bytes_basis"] == "decompressed"
    assert record["content_encoding"] == "gzip"
    assert record["tool_names"] == ["mcp__s__t"]  # the surface is read through the encoding
    assert recorder.unrecorded_requests == 0


def test_the_allowlist_is_confined_to_the_contract():
    # Growing it is a spec commit in 10-harness.md, never an implementation convenience:
    # the count-tokens entry (Q7) plus the five loop scalars added 2026-09-18, nothing else.
    assert list(RESPONSE_SCALAR_ALLOWLIST) == ["count_tokens", "chat_completions"]
    count = RESPONSE_SCALAR_ALLOWLIST["count_tokens"]
    assert set(count.suffixes) == {"/v1/messages/count_tokens", "/v1/responses/input_tokens"}
    assert [(s.key, s.pointer, s.kind) for s in count.scalars] == [("count_tokens", ("input_tokens",), "number")]
    chat = RESPONSE_SCALAR_ALLOWLIST["chat_completions"]
    assert chat.suffixes == ("/chat/completions",)
    assert [(s.key, s.path, s.kind) for s in chat.scalars] == [
        ("provider", "provider", "string"),
        ("usage_prompt_tokens", "usage.prompt_tokens", "number"),
        ("usage_completion_tokens", "usage.completion_tokens", "number"),
        ("usage_reasoning_tokens", "usage.completion_tokens_details.reasoning_tokens", "number"),
        ("usage_cost", "usage.cost", "number"),
    ]
    every_key = {s.key for e in RESPONSE_SCALAR_ALLOWLIST.values() for s in e.scalars}
    assert every_key == {"count_tokens", "provider", "usage_prompt_tokens", "usage_completion_tokens",
                         "usage_reasoning_tokens", "usage_cost"}


@pytest.mark.parametrize("path", ["/v1/messages/count_tokens", "/v1/responses/input_tokens"])
def test_count_tokens_scalar_lands_on_the_calling_request_only(tmp_path, fake_api_upstream, path):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        post(base, "/v1/messages", {"model": "m-1", "tools": [], "messages": []})
        response = post(base, path, {"model": "m-1", "messages": [{"role": "user", "content": "hi"}]})
        assert json.loads(response.read())["input_tokens"] == FAKE_COUNT_TOKENS_VALUE  # forwarded intact
        post(base, "/v1/messages", {"model": "m-1", "tools": [], "messages": []})
    finally:
        recorder.stop()
    lines = sorted(records(tmp_path), key=lambda r: r["seq"])
    assert [r["path"] for r in lines] == ["/v1/messages", path, "/v1/messages"]
    assert set(lines[0]) == REQUEST_RECORD_KEYS and set(lines[2]) == REQUEST_RECORD_KEYS
    assert set(lines[1]) == COUNT_TOKENS_KEYS
    assert lines[1]["count_tokens"] == FAKE_COUNT_TOKENS_VALUE
    assert lines[1]["count_tokens_note"] is None
    # No response body ever lands in the artifact -- only the one named scalar.
    raw = (tmp_path / "cap.jsonl").read_text()
    assert "fake-upstream" not in raw
    digest = summarize(tmp_path / "cap.jsonl", ("Bash",))
    assert digest["count_tokens_calls"] == 1
    assert digest["count_tokens_unread"] == 0
    assert digest["requests_recorded"] == 3


@pytest.mark.parametrize("encoding, decode", [("gzip", gzip.decompress), ("br", brotli.decompress)])
def test_count_tokens_is_read_through_the_negotiated_response_encoding(
        tmp_path, fake_api_upstream, encoding, decode):
    # DR-3 (2026-09-16): the upstream answered every observed count-tokens call
    # brotli-encoded and the recorder decoded only gzip/deflate. It may not narrow
    # the negotiation (that modifies traffic), so it decodes what was negotiated.
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        response = post(base, "/v1/messages/count_tokens", {"model": "m-1", "messages": []},
                        headers={"X-Fake-Response-Encoding": encoding})
        assert response.headers.get("Content-Encoding") == encoding  # forwarded as sent
        assert json.loads(decode(response.read()))["input_tokens"] == FAKE_COUNT_TOKENS_VALUE
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert record["count_tokens"] == FAKE_COUNT_TOKENS_VALUE
    assert record["count_tokens_note"] is None
    digest = summarize(tmp_path / "cap.jsonl", ("Bash",))
    assert (digest["count_tokens_calls"], digest["count_tokens_unread"]) == (1, 0)


def test_undecodable_response_encoding_is_an_unread_call_never_an_absent_one(tmp_path, fake_api_upstream):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        post(base, "/v1/messages/count_tokens", {"model": "m-1", "messages": []},
             headers={"X-Fake-Response-Encoding": "x-no-such-codec"})
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert set(record) == COUNT_TOKENS_KEYS
    assert record["count_tokens"] is None
    assert "x-no-such-codec" in record["count_tokens_note"]  # the encoding is named
    digest = summarize(tmp_path / "cap.jsonl", ("Bash",))
    assert digest["count_tokens_calls"] == 1
    assert digest["count_tokens_unread"] == 1


def test_brotli_request_body_is_measured_decompressed(tmp_path, fake_api_upstream):
    payload = {"model": "m-1", "tools": [{"name": "mcp__s__t"}], "messages": [{"role": "user", "content": "B" * 5000}]}
    serialized = json.dumps(payload).encode()
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        request = urllib.request.Request(base + "/v1/messages", data=brotli.compress(serialized),
                                         headers={"Content-Type": "application/json",
                                                  "Content-Encoding": "br"}, method="POST")
        urllib.request.urlopen(request, timeout=30)
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert (record["body_bytes"], record["body_bytes_basis"], record["content_encoding"]) == (
        len(serialized), "decompressed", "br")
    assert record["tool_names"] == ["mcp__s__t"]


def test_count_tokens_absent_scalar_is_null_with_the_reason(tmp_path, fake_api_upstream):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_api_upstream)
    base = recorder.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post(base, "/v1/messages/count_tokens", {"model": "m-1", "messages": []},
                 headers={"X-Fake-Status": "429"})
        assert exc.value.code == 429
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert record["count_tokens"] is None
    assert "429" in record["count_tokens_note"]


def test_count_tokens_on_a_dead_upstream_is_null_never_missing(tmp_path):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", "http://127.0.0.1:9")
    base = recorder.start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            post(base, "/v1/messages/count_tokens", {"model": "m-1", "messages": []})
    finally:
        recorder.stop()
    (record,) = records(tmp_path)
    assert set(record) == COUNT_TOKENS_KEYS
    assert record["count_tokens"] is None
    assert record["count_tokens_note"] == "upstream unreachable"
    assert recorder.forward_errors == 1


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
    # Pre-2026-09 records (no basis field) all parsed, so they read as readable.
    assert digest["requests_readable"] == 2
    assert digest["count_tokens_calls"] == 0
    assert digest["count_tokens_unread"] == 0
    assert summarize(tmp_path / "missing.jsonl", ("Bash",)) is None
