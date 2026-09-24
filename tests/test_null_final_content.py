"""Null-final content classification, transcripts, and answer artifacts."""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import replace

import pytest
from conftest import FakeDriver, manifest_data, write_manifest
from scenarios.loop_config import loop_config, loop_manifest
from scenarios.null_final import REASONING, experiment

from mcp_e2e_harness import loop_consumer
from mcp_e2e_harness.drivers.base import TurnSpec
from mcp_e2e_harness.loop_consumer import OfferedTool, TurnConfig, reasoning_tail_marks, run_turn
from mcp_e2e_harness.loop_scaffold import LOOP_SCAFFOLD_V1
from mcp_e2e_harness.manifest import load_manifest
from mcp_e2e_harness.runner import RunConfig, meta_relative_path, run
from mcp_e2e_harness.secrets import SecretLeakError

SCAFFOLD_HASH = "b92ad9f83c8f23bee833fd1cb4c746ff23c3b0cb9a2b4637edb1b8c2ce17e2f6"
TOOLS = [{"type": "function", "function": {
    "name": name, "parameters": {"type": "object", "properties": properties}}}
    for name, properties in [("search", {"query": {}, "page_size": {}}),
                             ("lookup", {"query": {}}), ("empty", {})]]


@pytest.mark.parametrize("message, expected, matches", [
    ({}, None, None),
    ({"reasoning": ""}, None, None),
    ({"reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]}, None, None),
    ({"reasoning": "some text"}, False, None),
    ({"reasoning": 'prefix {"query":"test"}  '}, True, ["lookup", "search"]),
    ({"reasoning": 'prefix {"query":"test", "page_size":5}'}, True, ["search"]),
    ({"reasoning": 'prefix {"unknown":1}'}, True, []),
    ({"reasoning": 'prefix {}'}, True, ["empty", "lookup", "search"]),
    ({"reasoning": 'prefix {"query":{"nested":[{"braces":"{}"}]}}'}, True, ["lookup", "search"]),
    ({"reasoning": 'prefix {"query":"escaped \\" } brace"}'}, True, ["lookup", "search"]),
    ({"reasoning": 'prefix {"query":1} trailing'}, False, None),
    ({"reasoning": 'prefix {"query":1,}'}, False, None),
    ({"reasoning": 'prefix {"query":NaN}'}, False, None),
    ({"reasoning": 'prefix {"query":1'}, False, None),
    ({"reasoning": 'prefix [{"query":1}]'}, False, None),
    ({"reasoning": 'prefix {"query":"unterminated}'}, False, None),
    ({"reasoning_details": [{"text": 'prefix {"query":'}, {"text": '"test"}'}]},
     True, ["lookup", "search"]),
    ({"reasoning_details": [{"text": "no object"}], "reasoning": '{"query":1}'}, False, None),
])
def test_reasoning_tail_marks(message, expected, matches):
    assert reasoning_tail_marks(message, TOOLS) == {
        "reasoning_tail_json": expected, "reasoning_tail_matches_tools": matches}


class ScriptedChat:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.requests = []
        self.retries = 0
        self.turn_tool_calls = 0

    def complete(self, body):
        self.requests.append(copy.deepcopy(body))
        message = next(self.messages)
        return {"choices": [{"message": message}], "usage": {}, "provider": "fixture"}


def turn_config(tmp_path):
    return TurnConfig(api_base="http://unused", api_key_env="UNUSED", model="fixture",
                      knobs={}, provider={}, provider_pin=None, pinned_provider_names=None,
                      scaffold="loop-scaffold@1", mode="turn", result_path=str(tmp_path / "result.json"))


@pytest.mark.parametrize("content", [None, "", {}, [], 42, False])
def test_nonanswer_content_is_not_serialized_and_is_not_step_cap(tmp_path, content):
    chat = ScriptedChat([{"role": "assistant", "content": content}])
    answer, result = run_turn(turn_config(tmp_path), LOOP_SCAFFOLD_V1, "prompt", chat, log=lambda _: None)
    assert answer == ""
    assert result["consumer_limit"]["cause"] == "null_final_content"
    assert result["consumer_limit"]["reasoning_tail_json"] is None
    assert result["consumer_limit"]["reasoning_tail_matches_tools"] is None
    assert result["step_cap_hit"] is False
    assert len(chat.requests) == 1 and result["retries"] == 0
    transcript = json.loads((tmp_path / "loop-session-single.json").read_text())
    assert transcript["messages"][-1]["content"] == content
    assert set(transcript) == {"messages", "offered_tools", "scaffold", "tool_definitions"}


@pytest.mark.parametrize("content", ["actual answer", "null", "None", " "])
def test_nonempty_string_is_preserved_verbatim(tmp_path, content):
    chat = ScriptedChat([{"role": "assistant", "content": content}])
    answer, result = run_turn(turn_config(tmp_path), LOOP_SCAFFOLD_V1, "prompt", chat, log=lambda _: None)
    assert answer == content and result["consumer_limit"] is None


def test_null_content_with_tool_call_executes_and_preserves_echo_policy(tmp_path, monkeypatch):
    class ToolClient:
        def call_tool(self, name, args):
            assert name == "lookup" and args == {"query": "x"}
            return {"response": {"content": [{"type": "text", "text": "found"}]},
                    "is_error": False, "response_bytes": 5}

    tool = OfferedTool("lookup", "sut", "lookup", ToolClient(), TOOLS[1])
    monkeypatch.setattr(loop_consumer, "offer_tools", lambda _: [tool])
    message = {"role": "assistant", "content": None, "reasoning": "plain transcript only",
               "reasoning_details": [{"text": "echoed carrier"}],
               "tool_calls": [{"id": "call", "function": {"name": "lookup", "arguments": '{"query":"x"}'}}]}
    chat = ScriptedChat([message, {"role": "assistant", "content": "answer"}])
    config = turn_config(tmp_path)
    answer, result = run_turn(config, LOOP_SCAFFOLD_V1, "prompt", chat, log=lambda _: None)
    assert answer == "answer" and result["consumer_limit"] is None
    assert result["tool_calls"] == 1 and result["steps"] == 2 and len(chat.requests) == 2
    echoed = chat.requests[1]["messages"][2]
    assert echoed["reasoning_details"] == message["reasoning_details"]
    assert "reasoning" not in echoed
    transcript_path = tmp_path / "loop-session-single.json"
    transcript = json.loads(transcript_path.read_text())
    assert transcript["messages"][2]["reasoning"] == "plain transcript only"
    # Recording plain reasoning also must not change a resumed request.
    resumed = ScriptedChat([{"role": "assistant", "content": "again"}])
    config.session_mode, config.session_path = "resume", str(transcript_path)
    run_turn(config, LOOP_SCAFFOLD_V1, "next", resumed, log=lambda _: None)
    assert "reasoning" not in resumed.requests[0]["messages"][2]
    assert LOOP_SCAFFOLD_V1.content_hash() == SCAFFOLD_HASH


def test_transcript_survives_failed_request(tmp_path):
    class FailedChat(ScriptedChat):
        def complete(self, body):
            raise loop_consumer.LoopError("fixture failure")

    with pytest.raises(loop_consumer.LoopError, match="fixture failure"):
        run_turn(turn_config(tmp_path), LOOP_SCAFFOLD_V1, "prompt", FailedChat([]), log=lambda _: None)
    transcript = json.loads((tmp_path / "loop-session-single.json").read_text())
    assert transcript["messages"][-1] == {"role": "user", "content": "prompt"}


@pytest.mark.parametrize("crowded", [False, True])
def test_null_final_acceptance_artifacts(tmp_path, crowded):
    config, result = experiment(tmp_path, crowded)
    assert result.failures == 0 and result.voided_cells == {} and result.zero_trace_cells == []
    assert result.consumer_limits == {"loop-cell/A/A1/r02": "null_final_content"}
    meta = result.results[1]
    row = config.run_dir / meta_relative_path(config, meta)
    assert (row / "answer.txt").read_bytes() == b""
    assert meta["harness_failure"] is None and meta["answer_chars"] == 0
    assert meta["answer_sha256_16"] is None
    outcome = meta["consumer_limit"]
    assert outcome["cause"] == "null_final_content" and outcome["reasoning_tail_json"] is True
    expected = ["mcp__notes_sut__file_note"]
    if crowded:
        expected.append("mcp__shared_notes__file_note")
    assert outcome["reasoning_tail_matches_tools"] == expected
    assert meta["loop"]["finish_reason"] == "stop"
    assert meta["loop"]["retries"] == 0 and meta["loop"]["steps"] == 2
    manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    answers = manifest["cell_marks"]["loop-cell"]["answers"]["A1"]
    assert (answers["invocations"], answers["answered"], answers["distinct"]) == (3, 2, 1)
    assert sum(answers["digests"].values()) == 2
    assert manifest["consumer_limits"] == result.consumer_limits
    assert "null_final_content" in (tmp_path / "progress.txt").read_text()
    for meta in result.results:
        dest = config.run_dir / meta_relative_path(config, meta)
        transcripts = list(dest.glob("loop-session-*.json"))
        assert len(transcripts) == 1
        transcript = json.loads(transcripts[0].read_text())
        assert set(transcript) == {"messages", "offered_tools", "scaffold", "tool_definitions"}
        assert meta["scaffold"]["content_hash"] == SCAFFOLD_HASH
    # Reasoning appears only in the consumer transcript, never the wire or metadata.
    marker_paths = [p for p in config.run_dir.rglob("*") if p.is_file()
                    and "WO7 reasoning-only marker" in p.read_text(errors="replace")]
    assert marker_paths == list(row.glob("loop-session-*.json"))
    transcript = json.loads(marker_paths[0].read_text())
    assert transcript["messages"][-1]["reasoning_details"][0]["text"] == REASONING


def product_config(tmp_path, file_answer=False, *, failure=False):
    class ProductFixture(FakeDriver):
        def build_turn(self, ctx):
            if failure:
                return TurnSpec([sys.executable, "-c", "raise SystemExit(2)"], stdin_text=ctx.prompt)
            turn = super().build_turn(ctx)
            if file_answer:
                answer_path = ctx.dest / "agent-last-message.txt"
                return replace(turn, answer_from="file", answer_path=answer_path,
                               env_overrides={"FAKE_ANSWER_FILE": str(answer_path)})
            return turn

    data = manifest_data()
    data["cells"]["basic"]["merge_gating"] = False
    return RunConfig(load_manifest(write_manifest(tmp_path, data)), tmp_path / "run", ["basic"],
                     drivers={"fake": ProductFixture()}, log=lambda _: None)


@pytest.mark.parametrize("file_answer", [False, True])
def test_product_empty_answer_has_null_digest_and_consumer_outcome(tmp_path, monkeypatch, file_answer):
    monkeypatch.setenv("FAKE_ANSWER_TEXT", "")
    config = product_config(tmp_path, file_answer)
    result = run(config)
    meta = result.results[0]
    assert result.failures == 0 and not result.voided_cells
    assert meta["answer_chars"] == 0 and meta["answer_sha256_16"] is None
    assert meta["harness_failure"] is None
    assert meta["consumer_limit"]["cause"] == "null_final_content"
    assert meta["consumer_limit"]["reasoning_tail_json"] is None
    assert (config.run_dir / "basic/A/A1/answer.txt").read_bytes() == b""
    manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert manifest["cell_marks"]["basic"]["answers"]["A1"] == {
        "invocations": 1, "answered": 0, "distinct": 0, "digests": {}}


def test_failed_product_is_not_reclassified_as_null_content(tmp_path):
    result = run(product_config(tmp_path, failure=True))
    meta = result.results[0]
    assert result.failures > 0 and meta["harness_failure"] is not None
    assert meta["consumer_limit"] is None and meta["answer_sha256_16"] is None


@pytest.mark.parametrize("broken_surface", [False, True])
def test_call_free_null_final_is_not_voided(tmp_path, monkeypatch, broken_surface):
    from fake_openrouter import FakeOpenRouter

    original = FakeOpenRouter.answer

    def null_without_call(self, body):
        status, response = original(self, body)
        if not self._is_probe(body):
            response["choices"][0]["message"] = {"role": "assistant", "content": None}
            response["choices"][0]["finish_reason"] = "stop"
        return status, response

    monkeypatch.setattr(FakeOpenRouter, "answer", null_without_call)
    if broken_surface:
        monkeypatch.setattr("mcp_e2e_harness.runner.expected_wire_surface", lambda *args: ["unexpected"])
    config, result = experiment(tmp_path)
    if broken_surface:
        assert result.failures > 0 and result.voided_cells
        assert "wire tools array differs" in result.results[0]["harness_failure"]
        return
    assert result.failures == 0 and result.zero_trace_cells == [] and result.voided_cells == {}
    assert all(m["trace_records"] == 0 and m["consumer_limit"]["cause"] == "null_final_content"
               for m in result.results)
    assert not list(config.run_dir.rglob("CELL-VOID.json"))


@pytest.mark.parametrize("crowded", [False, True])
def test_transcript_secret_scan_includes_reasoning(tmp_path, monkeypatch, crowded):
    from fake_openrouter import FakeOpenRouter

    secret = "wo7-sensitive-reasoning-123456"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    fake = FakeOpenRouter()
    original = fake.answer

    def secret_reasoning(body):
        status, response = original(body)
        if not fake._is_probe(body):
            response["choices"][0]["message"]["reasoning_details"] = [{"text": secret}]
        return status, response

    fake.answer = secret_reasoning
    monkeypatch.setenv("MCP_E2E_OPENROUTER_UPSTREAM", fake.start())
    try:
        data = loop_manifest()
        if crowded:
            data["cells"]["loop-cell"].update(context="crowded", crowding={
                "procedure": "neutral-file-triage@2", "collision_review": "fixture disjoint tools"})
        config = loop_config(tmp_path, data)
        with pytest.raises(SecretLeakError, match="loop-session"):
            run(config)
        assert not (config.run_dir / "run-manifest.json").exists()
    finally:
        fake.stop()


@pytest.mark.parametrize("content", [None, ""])
def test_loop_null_and_empty_content_without_reasoning(tmp_path, monkeypatch, content):
    from fake_openrouter import FakeOpenRouter

    fake = FakeOpenRouter()
    fake.answer_text = content
    monkeypatch.setenv("OPENROUTER_API_KEY", "wo7-test-key-0123456789")
    monkeypatch.setenv("MCP_E2E_OPENROUTER_UPSTREAM", fake.start())
    try:
        config = loop_config(tmp_path, loop_manifest())
        result = run(config)
        meta = result.results[0]
        assert result.failures == 0 and not result.voided_cells
        assert meta["answer_chars"] == 0 and meta["answer_sha256_16"] is None
        assert meta["consumer_limit"]["cause"] == "null_final_content"
        assert meta["consumer_limit"]["reasoning_tail_json"] is None
        assert meta["consumer_limit"]["reasoning_tail_matches_tools"] is None
        assert meta["loop"]["finish_reason"] == "stop"
        assert meta["loop"]["steps"] == 2 and len(fake.scored_requests) == 2
    finally:
        fake.stop()


def test_missing_product_answer_file_is_an_instrument_failure(tmp_path):
    class MissingFile(FakeDriver):
        def build_turn(self, ctx):
            return replace(super().build_turn(ctx), answer_from="file", answer_path=ctx.dest / "missing.txt")

    config = product_config(tmp_path)
    config.drivers = {"fake": MissingFile()}
    result = run(config)
    assert result.failures > 0
    assert "answer file missing" in result.results[0]["harness_failure"]
    assert result.results[0]["consumer_limit"] is None


@pytest.mark.parametrize("response", [{}, {"choices": [{"message": "malformed"}]}])
def test_missing_assistant_message_is_not_a_consumer_outcome(tmp_path, response):
    class BadResponse(ScriptedChat):
        def complete(self, body):
            return response

    with pytest.raises(loop_consumer.LoopError, match="no assistant message"):
        run_turn(turn_config(tmp_path), LOOP_SCAFFOLD_V1, "prompt", BadResponse([]), log=lambda _: None)
    assert (tmp_path / "loop-session-single.json").exists()
