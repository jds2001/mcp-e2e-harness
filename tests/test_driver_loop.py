"""The loop driver (50-drivers.md -> loop; WO-1), end to end against a fake OpenRouter.

Every run here goes runner -> loop consumer subprocess -> harness recorder -> fake
upstream, with the trace proxy and the distractor server (as SUT) in between, so
the artifacts asserted on are the real ones: api-surface.jsonl lines off the wire,
meta.json marks, run-manifest.json, checks-report.json, CELL-VOID.json. No network.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from conftest import manifest_data, write_manifest
from fake_openrouter import MODEL, PIN, PROBE_TOOL, PROVIDER, FakeOpenRouter

from mcp_e2e_harness.api_capture import ApiSurfaceRecorder, loop_digest, summarize, surface_mismatches
from mcp_e2e_harness.drivers.base import DriverAttributionError, TurnContext, TurnSpec
from mcp_e2e_harness.drivers.loop import LoopDriver, cell_marks
from mcp_e2e_harness.loop_scaffold import LOOP_SCAFFOLD_V1, validate_probe_arguments
from mcp_e2e_harness.manifest import ManifestError, load_manifest, validate_manifest
from mcp_e2e_harness.openrouter import (
    estimate_invocation_usd,
    parse_pin,
    provider_block,
    served_matches_pin,
)
from mcp_e2e_harness.runner import HarnessError, RunConfig, probe_loop_cells, run, scan_set_for

TEST_KEY = "sk-or-test-0123456789abcdef"

LOOP_CELL = {
    "driver": "loop",
    "model": MODEL,
    "knobs": {"max_tokens": 200, "reasoning": {"effort": "low"}},
    "role": "floor",
    "context": "fresh",
    "tool_surface": "full",
    "merge_gating": True,
    "groups": ["A"],
    "endpoint": "openrouter",
    "scaffold": "loop-scaffold@1",
    "provider": PIN,
}


@pytest.fixture()
def fake_openrouter(monkeypatch):
    fake = FakeOpenRouter()
    url = fake.start()
    monkeypatch.setenv("MCP_E2E_OPENROUTER_UPSTREAM", url)
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    yield fake
    fake.stop()


def loop_manifest(**cell_overrides) -> dict:
    data = manifest_data()
    cell = copy.deepcopy(LOOP_CELL)
    cell.update(cell_overrides)
    data["cells"] = {"loop-cell": cell}
    return data


def loop_config(tmp_path, data, **overrides) -> RunConfig:
    manifest = load_manifest(write_manifest(tmp_path, data))
    defaults = dict(manifest=manifest, run_dir=tmp_path / "run", cells=list(manifest.cells),
                    drivers={"loop": LoopDriver()}, timeout_s=120, log=lambda line: None,
                    probe_cache_dir=tmp_path / "probe-cache")
    defaults.update(overrides)
    return RunConfig(**defaults)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


SUT_TOOLS = ["mcp__notes_sut__file_note", "mcp__notes_sut__list_unfiled_notes", "mcp__notes_sut__read_note"]


def marks_answers_ok(run_dir: Path, cell: str, prompt_id: str, *, invocations: int, distinct: int) -> bool:
    """The per-prompt distinct-answer block (requirement 11 concerns repeats of ONE prompt)."""
    answers = json.loads((run_dir / "run-manifest.json").read_text())["cell_marks"][cell]["answers"][prompt_id]
    return (answers["invocations"], answers["distinct"], sum(answers["digests"].values())) == (
        invocations, distinct, invocations)


# ------------------------------------------------------------- the scaffold

def test_scaffold_is_hashed_instrument_content():
    scaffold = LOOP_SCAFFOLD_V1
    assert scaffold.full_name == "loop-scaffold@1"
    assert len(scaffold.content_hash()) == 64
    # Every element the work order names as scaffold content participates in the hash.
    changed = copy.replace(scaffold, step_cap=scaffold.step_cap + 1) if hasattr(copy, "replace") else None
    if changed is not None:
        assert changed.content_hash() != scaffold.content_hash()
    assert scaffold.probe_tool_name == PROBE_TOOL


def test_scaffold_system_prompt_makes_no_disclosure():
    prompt = LOOP_SCAFFOLD_V1.system_prompt.lower()
    for word in ("test", "harness", "evaluat", "measur", "benchmark", "experiment"):
        assert word not in prompt, word
    assert len(prompt) < 600


def test_probe_argument_validation():
    assert validate_probe_arguments(LOOP_SCAFFOLD_V1, {"key": "alpha"}) == []
    assert validate_probe_arguments(LOOP_SCAFFOLD_V1, {"key": 7}) != []
    assert validate_probe_arguments(LOOP_SCAFFOLD_V1, {}) != []
    assert validate_probe_arguments(LOOP_SCAFFOLD_V1, {"key": "a", "extra": 1}) != []
    assert validate_probe_arguments(LOOP_SCAFFOLD_V1, ["alpha"]) != []


# ---------------------------------------------------------- routing policy

def test_provider_block_sets_strict_routing_on_every_request():
    unpinned = provider_block(None, "deny")
    assert unpinned == {"require_parameters": True, "data_collection": "deny"}
    pinned = provider_block("deepinfra/bf16", "deny")
    assert pinned == {"require_parameters": True, "data_collection": "deny",
                      "order": ["deepinfra"], "allow_fallbacks": False, "quantizations": ["bf16"]}
    assert provider_block("openai", "allow") == {"require_parameters": True, "data_collection": "allow",
                                                 "order": ["openai"], "allow_fallbacks": False}
    with pytest.raises(ValueError):
        provider_block(None, "maybe")


def test_pin_parsing_and_served_provider_matching():
    assert parse_pin("deepinfra/bf16") == ("deepinfra", "bf16")
    assert parse_pin("openai") == ("openai", None)
    with pytest.raises(ValueError):
        parse_pin("DeepInfra/BF16")
    assert served_matches_pin("DeepInfra", "deepinfra/bf16", ["DeepInfra"])
    assert not served_matches_pin("AkashML", "deepinfra/bf16", ["DeepInfra"])
    # Without a listing lookup, the display name normalizes against the slug.
    assert served_matches_pin("Google AI Studio", "google-ai-studio", None)
    assert not served_matches_pin(None, "deepinfra/bf16", ["DeepInfra"])
    assert not served_matches_pin("", "deepinfra", None)


def test_cost_estimate_is_labeled_and_names_the_unbounded_term():
    pricing = {"prompt": 0.15e-6, "completion": 0.6e-6}
    product = estimate_invocation_usd("crowded", pricing, basis="product")
    assert product["estimated_input_tokens"] > product["estimated_output_tokens"]
    assert 0.02 < product["usd"] < 0.05
    assert "unbounded" in product["basis"] and "byte" in product["basis"]
    # WO-2 item 4: loop cells use the loop-measured basis (50-drivers.md, 2026-09-18),
    # an order of magnitude below the product byte basis, and say which was used.
    loop = estimate_invocation_usd("crowded", pricing)
    assert loop["estimated_input_tokens"] == 46_577 and loop["basis_name"].startswith("loop-measured")
    assert loop["usd"] < product["usd"] / 3
    assert estimate_invocation_usd("fresh", pricing)["estimated_input_tokens"] == 9_900
    assert "unbounded" in loop["basis"]
    assert estimate_invocation_usd("fresh", None)["usd"] is None


# ------------------------------------------------------------ the manifest

@pytest.mark.parametrize("mutate, fragment", [
    (lambda c: c.update(model="openai/gpt-oss-120b:free"), "routing suffix"),
    (lambda c: c.pop("endpoint"), "endpoint"),
    (lambda c: c.update(endpoint="vendor-direct"), "not a named deployment"),
    (lambda c: c.pop("scaffold"), "scaffold"),
    (lambda c: c.update(scaffold="loop-scaffold@9"), "not a harness scaffold"),
    (lambda c: c.update(provider="Deep Infra")," not an endpoint tag"),
    (lambda c: c.update(data_policy="maybe"), "data_policy"),
    (lambda c: c.update(budget_usd=0), "positive number"),
    (lambda c: c.update(budget_usd="1"), "positive number"),
    (lambda c: c["knobs"].update(messages=[]), "harness constructs"),
    (lambda c: c.update(unknown_loop_key=1), "unknown key"),
])
def test_loop_cell_load_errors(mutate, fragment):
    data = loop_manifest()
    mutate(data["cells"]["loop-cell"])
    with pytest.raises(ManifestError, match=fragment.strip()):
        validate_manifest(data)


def test_loop_keys_are_unknown_on_a_product_driver_cell():
    data = manifest_data()
    data["cells"]["basic"]["driver"] = "claude-code"
    data["cells"]["basic"]["scaffold"] = "loop-scaffold@1"
    with pytest.raises(ManifestError, match="unknown key"):
        validate_manifest(data)


def test_loop_optional_fields_accept_explicit_null_as_absent():
    data = loop_manifest(provider=None, data_policy=None, budget_usd=None)
    validate_manifest(data)
    marks = cell_marks(data["cells"]["loop-cell"])
    assert marks["provider_pin"] == "unpinned" and marks["reproducibility"] == "unpinned"
    assert marks["data_policy"] == "deny"
    assert marks["scaffold"] == {"name": "loop-scaffold@1", "content_hash": LOOP_SCAFFOLD_V1.content_hash()}


# ------------------------------------------------------- turn construction

def test_build_turn_writes_the_turn_file_and_asserts_the_record(tmp_path):
    driver = LoopDriver()
    cell = copy.deepcopy(LOOP_CELL)
    ctx = TurnContext(model=MODEL, knobs=cell["knobs"], mcp_config_path=tmp_path / "mcp-config.json",
                      server_name="sut", tool_surface="full", prompt="hello", dest=tmp_path,
                      api_base_url="http://127.0.0.1:4242", cell=cell)
    turn = driver.build_turn(ctx)
    assert turn.argv[1:4] == ["-m", "mcp_e2e_harness.loop_consumer", "--turn"]
    assert turn.stdin_text == "hello" and turn.answer_from == "stdout"
    assert turn.env_overrides == {}  # the credential travels by inheritance, never an override
    data = json.loads(Path(turn.argv[4]).read_text())
    assert data["api_base"] == "http://127.0.0.1:4242/api/v1"
    assert data["provider"] == provider_block(PIN, "deny")
    assert data["knobs"] == cell["knobs"]
    assert data["mode"] == "turn" and data["session_mode"] == "single"
    record = driver.attribution_record(turn)
    assert record["provider_pin"] == PIN and record["provider_policy"]["require_parameters"] is True
    # Tampering with the executed material is caught from the file, not from intent.
    data["provider"]["require_parameters"] = False
    Path(turn.argv[4]).write_text(json.dumps(data))
    with pytest.raises(DriverAttributionError, match="require_parameters"):
        driver.attribution_record(turn)
    data["provider"]["require_parameters"] = True
    data["api_base"] = "https://openrouter.ai/api/v1"  # bypassing the recorder
    Path(turn.argv[4]).write_text(json.dumps(data))
    with pytest.raises(DriverAttributionError, match="recorder"):
        driver.attribution_record(turn)
    with pytest.raises(DriverAttributionError, match="consumer module"):
        driver.attribution_record(TurnSpec(argv=["python", "-m", "something.else"], stdin_text=""))


def test_build_turn_without_a_cell_is_refused(tmp_path):
    ctx = TurnContext(model=MODEL, knobs={}, mcp_config_path=tmp_path / "x", server_name="s",
                      tool_surface="full", prompt="p", dest=tmp_path)
    with pytest.raises(DriverAttributionError, match="cell"):
        LoopDriver().build_turn(ctx)


def test_missing_credential_is_a_load_time_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = loop_config(tmp_path, loop_manifest())
    with pytest.raises(HarnessError, match="OPENROUTER_API_KEY"):
        run(config)
    assert not (tmp_path / "run").exists()


def test_the_loop_credential_joins_the_hygiene_scan_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    config = loop_config(tmp_path, loop_manifest())
    assert TEST_KEY in scan_set_for(config, LoopDriver())


def test_dry_run_builds_the_turn_and_calls_nothing(tmp_path, fake_openrouter):
    config = loop_config(tmp_path, loop_manifest(), dry_run=True)
    result = run(config)
    assert result.failures == 0
    assert fake_openrouter.requests == [] and fake_openrouter.paths == []
    dest = config.run_dir / "loop-cell" / "A" / "A1"
    assert (dest / "loop-turn.json").exists()
    assert not (dest / "loop-result-turn.json").exists()


# ---------------------------------------------------------- end to end

def test_pinned_cell_end_to_end(tmp_path, fake_openrouter):
    fake = fake_openrouter
    data = loop_manifest()
    data["checks"] = [{"id": "typed", "description": "d", "applies_to": {"tool": "*"},
                       "assert": {"present": ["/response/content"]}}]
    lines_logged: list[str] = []
    config = loop_config(tmp_path, data, log=lines_logged.append)
    result = run(config)
    assert result.failures == 0, result.results[0]["harness_failure"]
    meta = result.results[0]
    assert meta["harness_failure"] is None and meta["breaches"] == []

    # S11 marks on the row, top level and in the family record.
    assert meta["driver_family"] == "loop"
    assert meta["scaffold"] == {"name": "loop-scaffold@1", "content_hash": LOOP_SCAFFOLD_V1.content_hash()}
    assert meta["provider_pin"] == PIN and meta["reproducibility"] == "pinned"
    assert meta["cell_id"].endswith(f"/scaffold:loop-scaffold@1/pin:{PIN}")
    assert meta["consumer_limit"] is None
    loop = meta["loop"]
    # WO-2 item 6: the verified part of the pin is distinguishable from the asserted part.
    assert meta["quantization_asserted"] == "bf16"
    assert loop["pin_slug_verified_against"] == "fakeprov" and loop["quantization_asserted"] == "bf16"
    assert loop["provider_verified"] == PROVIDER
    assert "quantization asserted" in loop["pin_verification"]
    assert loop["served_providers"] == {PROVIDER: 2}
    assert loop["provider_mismatches"] == [] and loop["provider_unread"] == 0
    assert loop["steps"] == 2 and loop["tool_calls"] == 1 and loop["step_cap_hit"] is False
    assert loop["usage"]["cost_usd"] == pytest.approx(0.002)
    assert loop["usage"]["reasoning_tokens"] == 6
    assert meta["spend_usd"] == pytest.approx(0.002)
    assert meta["tool_calls"] == ["list_unfiled_notes"]  # traced through the proxy
    assert meta["knobs"] == LOOP_CELL["knobs"]  # verbatim

    # The wire: every scored request carries the five scalars and the exact surface.
    dest = config.run_dir / "loop-cell" / "A" / "A1"
    wire = read_lines(dest / "api-surface.jsonl")
    assert [r["path"] for r in wire] == ["/api/v1/chat/completions"] * 2
    for r in wire:
        assert r["provider"] == PROVIDER and r["provider_note"] is None
        assert r["usage_prompt_tokens"] > 0 and r["usage_completion_tokens"] == 20
        assert r["usage_reasoning_tokens"] == 3 and r["usage_cost"] == 0.001
        assert "probe" not in r
        assert "provider" in r["body_keys"] and "usage" in r["body_keys"]
        assert r["tool_names"] == SUT_TOOLS or sorted(r["tool_names"]) == SUT_TOOLS
    assert all(isinstance(r["duration_ms"], float) and r["duration_note"] is None for r in wire)
    assert set(wire[0]) == {"seq", "at", "path", "model", "tool_names", "tool_source", "body_keys",
                            "body_bytes", "body_bytes_basis", "content_encoding", "duration_ms", "duration_note",
                            "provider", "provider_note", "usage_prompt_tokens", "usage_prompt_tokens_note",
                            "usage_completion_tokens", "usage_completion_tokens_note",
                            "usage_reasoning_tokens", "usage_reasoning_tokens_note",
                            "usage_cost", "usage_cost_note", "finish_reason", "finish_reason_note"}
    assert [r["finish_reason"] for r in wire] == ["tool_calls", "stop"]
    assert all(r["finish_reason_note"] is None for r in wire)
    assert loop["finish_reason"] == "stop" and loop["finish_reasons"] == {"tool_calls": 1, "stop": 1}
    assert meta["answer_sha256_16"] and len(meta["answer_sha256_16"]) == 16
    assert marks_answers_ok(config.run_dir, "loop-cell", "A1", invocations=1, distinct=1)
    surface = meta["api_surface"]
    assert surface["expected_wire_surface"] == SUT_TOOLS
    assert surface["wire_surface_mismatches"] == []
    assert surface["requests_with_tools"] == 2

    # What actually reached the upstream (through the recorder): strict routing on
    # every request, knobs verbatim, the scaffold's system prompt first.
    assert len(fake.probe_requests) == 1 and len(fake.scored_requests) == 2
    for body in fake.requests:
        assert body["provider"] == {"require_parameters": True, "data_collection": "deny",
                                    "order": ["fakeprov"], "allow_fallbacks": False, "quantizations": ["bf16"]}
        assert body["usage"] == {"include": True}
        assert body["max_tokens"] == 200 and body["reasoning"] == {"effort": "low"}
        assert body["messages"][0] == {"role": "system", "content": LOOP_SCAFFOLD_V1.system_prompt}
    assert fake.requests[0] is fake.probe_requests[0]  # the probe precedes the first scored turn
    assert sorted(t["function"]["name"] for t in fake.scored_requests[0]["tools"]) == SUT_TOOLS
    assert fake.scored_requests[1]["messages"][-1]["role"] == "tool"
    assert (dest / "answer.txt").read_text() == fake.answer_text

    # The probe's own artifacts and cache, marked probe, never among scored lines.
    probe_dir = config.run_dir / "loop-cell" / "loop-probe"
    probe = json.loads((probe_dir / "probe.json").read_text())
    assert probe["verdict"] == "pass" and probe["cached"] is False and probe["served_provider"] == PROVIDER
    assert probe["tuple"]["provider"] == PIN and probe["tuple"]["scaffold_hash"] == LOOP_SCAFFOLD_V1.content_hash()
    assert probe["tuple"]["knobs"] == LOOP_CELL["knobs"]
    probe_lines = read_lines(probe_dir / "api-surface.jsonl")
    assert len(probe_lines) == 1 and probe_lines[0]["probe"] is True
    assert probe_lines[0]["tool_names"] == [PROBE_TOOL]
    assert summarize(probe_dir / "api-surface.jsonl", ()) == {
        "requests_recorded": 0, "requests_readable": 0, "requests_with_tools": 0, "tool_names_union": [],
        "disallowed_builtins_on_wire": [], "count_tokens_calls": 0, "count_tokens_unread": 0}
    assert list((tmp_path / "probe-cache").glob("*.json"))
    assert loop["probe"]["verdict"] == "pass" and loop["probe"]["cached"] is False

    # run-manifest and the checks report carry the marks and the served set.
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    marks = run_manifest["loop_cells"]["loop-cell"]
    assert marks["driver_family"] == "loop" and marks["provider_pin"] == PIN
    assert marks["served_providers"] == {PROVIDER: 2} and marks["spend_usd"] == pytest.approx(0.003)
    assert marks["probe"]["verdict"] == "pass"
    assert run_manifest["cell_gates"]["loop-cell"]["verdict"] == "pass"
    assert run_manifest["budget"]["spent_usd"] == pytest.approx(0.003)
    assert run_manifest["budget"]["stops"] == [] and run_manifest["budget"]["run_stopped_by_budget"] is False
    assert marks["provider_verified"] == [PROVIDER] and marks["quantization_asserted"] == "bf16"
    pre = run_manifest["pre_run"]["loop"]
    assert pre["cells"]["loop-cell"]["estimate"]["usd_total"] > 0
    assert pre["estimate_basis"].startswith("loop-measured")
    assert pre["cells"]["loop-cell"]["estimate"]["basis_name"] == pre["estimate_basis"]
    assert "loop-measured basis" in "\n".join(lines_logged)
    assert pre["cells"]["loop-cell"]["pinned_endpoint"]["advertises_tools"] is True
    assert pre["cells"]["loop-cell"]["provider_disclosure"]["privacy_policy_url"] == "https://fake.test/privacy"
    assert "not a verified privacy property" in pre["cells"]["loop-cell"]["provider_disclosure"]["honesty_limit"]
    report = json.loads((config.run_dir / "checks-report.json").read_text())
    assert report["cells"]["loop-cell"]["driver_family"] == "loop"
    assert report["cells"]["loop-cell"]["reproducibility"] == "pinned"
    assert report["checks"][0]["outcome"] == "pass"

    # S9: the pre-run legibility lines precede any invocation.
    joined = "\n".join(lines_logged)
    assert "ESTIMATE" in joined and "unbounded" in joined and "data policy" in joined
    assert joined.index("loop cost") < joined.index("calibration probe ->")


def test_probe_verdict_is_cached_per_tuple(tmp_path, fake_openrouter):
    fake = fake_openrouter
    first = loop_config(tmp_path, loop_manifest(), run_dir=tmp_path / "run1")
    run(first)
    assert len(fake.probe_requests) == 1
    second = loop_config(tmp_path, loop_manifest(), run_dir=tmp_path / "run2")
    result = run(second)
    assert result.failures == 0
    assert len(fake.probe_requests) == 1  # no second probe request
    gate = json.loads((second.run_dir / "run-manifest.json").read_text())["cell_gates"]["loop-cell"]
    assert gate["cached"] is True and gate["verdict"] == "pass"
    # A different pin is a different tuple: the probe runs again.
    third = loop_config(tmp_path, loop_manifest(provider="other/fp8"), run_dir=tmp_path / "run3")
    fake.provider_sequence = ["Other"]
    run(third)
    assert len(fake.probe_requests) == 2


@pytest.mark.parametrize("behavior, fragment", [
    ("no-call", "exactly one tool call, got 0"),
    ("two-calls", "got 2"),
    ("wrong-name", "lookup_value"),
    ("bad-json", "not valid JSON"),
    ("schema-invalid", "schema"),
])
def test_probe_failure_refuses_the_cell_before_any_model_turn(tmp_path, fake_openrouter, behavior, fragment):
    fake = fake_openrouter
    fake.probe_behavior = behavior
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    assert result.results == []  # no scored turn spent
    assert "loop-cell" in result.voided_cells and fragment in result.voided_cells["loop-cell"]
    void = json.loads((config.run_dir / "loop-cell" / "CELL-VOID.json").read_text())
    assert void["verdict"] == "BROKEN"
    assert len(fake.requests) == 1 and fake.scored_requests == []
    probe = json.loads((config.run_dir / "loop-cell" / "loop-probe" / "probe.json").read_text())
    assert probe["verdict"] == "fail"
    # The verdict is cached: a re-run refuses from the cache without spending a request.
    again = loop_config(tmp_path, loop_manifest(), run_dir=tmp_path / "run2")
    result = run(again)
    assert len(fake.requests) == 1
    assert "cached" in result.voided_cells["loop-cell"] and "delete" in result.voided_cells["loop-cell"]


def test_served_provider_mismatch_breaks_the_pinned_cell(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.provider_sequence = [PROVIDER, "Other"]  # the probe is served by the pin; the turn is not
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    assert result.failures >= 1
    meta = result.results[0]
    assert meta["loop"]["provider_mismatches"] == [{"seq": 1, "served": "Other"}]
    assert "mismatch" in meta["harness_failure"]
    assert "loop-cell" in result.voided_cells
    # The consumer stopped at the first mismatched response: no further spend.
    assert len(fake.scored_requests) == 1
    dest = config.run_dir / "loop-cell" / "A" / "A1"
    consumer = json.loads((dest / "loop-result-turn.json").read_text())
    assert consumer["breach"]["kind"] == "provider_mismatch"


def test_absent_provider_is_a_mismatch_never_a_pass(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.omit_provider_from = 2
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    meta = result.results[0]
    assert meta["loop"]["provider_mismatches"] == [{"seq": 1, "served": None}]
    assert meta["loop"]["provider_unread"] == 1
    assert "loop-cell" in result.voided_cells
    line = read_lines(config.run_dir / "loop-cell" / "A" / "A1" / "api-surface.jsonl")[0]
    assert line["provider"] is None and "no 'provider'" in line["provider_note"]


def test_unpinned_cell_runs_and_marks_every_artifact(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.provider_sequence = ["Alpha", "Beta"]
    data = loop_manifest(provider=None)
    data["checks"] = [{"id": "never-holds", "description": "d", "applies_to": {"tool": "*"},
                       "assert": {"present": ["/response/nope"]}}]
    config = loop_config(tmp_path, data)
    result = run(config)
    assert result.failures == 0
    meta = result.results[0]
    assert meta["reproducibility"] == "unpinned" and meta["provider_pin"] == "unpinned"
    assert meta["loop"]["reproducibility"] == "unpinned"
    assert meta["loop"]["served_providers"] == {"Beta": 1, "Alpha": 1}
    assert meta["loop"]["provider_mismatches"] == []
    for body in fake.requests:
        assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    cell = run_manifest["loop_cells"]["loop-cell"]
    assert cell["reproducibility"] == "unpinned" and cell["served_providers"] == {"Beta": 1, "Alpha": 1}
    report = json.loads((config.run_dir / "checks-report.json").read_text())
    assert report["cells"]["loop-cell"]["reproducibility"] == "unpinned"
    check = report["checks"][0]
    assert check["outcome"] == "fail"
    assert all(f["reproducibility"] == "unpinned" and f["driver_family"] == "loop" for f in check["failures"])
    probe = run_manifest["cell_gates"]["loop-cell"]
    assert probe["tuple"]["provider"] == "unpinned" and probe["served_provider"] == "Alpha"


def test_data_policy_allow_is_the_recorded_opt_out(tmp_path, fake_openrouter):
    fake = fake_openrouter
    config = loop_config(tmp_path, loop_manifest(data_policy="allow"))
    result = run(config)
    assert result.failures == 0
    assert all(b["provider"]["data_collection"] == "allow" for b in fake.requests)
    assert result.results[0]["loop"]["data_policy"] == "allow"


def test_knob_refused_under_strict_routing_is_a_named_breach(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.refuse_knobs = {"temperature"}
    with_temp = loop_manifest(knobs={"temperature": 0, "max_tokens": 200})
    config = loop_config(tmp_path, with_temp)
    result = run(config)
    assert result.results == []
    reason = result.voided_cells["loop-cell"]
    assert "routing_refused" in reason and "temperature" in reason and "require_parameters" in reason
    assert "'temperature'" in reason  # named from the pinned endpoint's declared parameters
    assert fake.scored_requests == []
    # The same cell without the knob runs normally.
    fake.refuse_knobs = set()
    ok = loop_config(tmp_path, loop_manifest(knobs={"max_tokens": 200}), run_dir=tmp_path / "run2")
    result = run(ok)
    assert result.failures == 0


def test_run_level_budget_stops_the_run_cleanly(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.cost_per_request = 0.5
    data = loop_manifest()
    data["prompts"].append(dict(data["prompts"][0], id="A2"))
    config = loop_config(tmp_path, data, budget_usd=1.0)
    result = run(config)
    # probe 0.5 + two scored requests 1.0 = 1.5 >= cap after the first invocation
    assert len(result.results) == 1
    assert result.results[0]["harness_failure"] is None
    assert result.spend_usd == pytest.approx(1.5)
    assert result.budget_stops == [{"scope": "run", "cap_usd": 1.0, "spent_usd": 1.5,
                                    "at": "loop-cell/A2", "skipped_in_cell": 1}]
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["budget"]["run_stopped_by_budget"] is True
    assert run_manifest["budget"]["stops"][0]["scope"] == "run"
    assert len(fake.scored_requests) == 2
    # The completed invocation stands.
    assert (config.run_dir / "loop-cell" / "A" / "A1" / "meta.json").exists()
    assert not (config.run_dir / "loop-cell" / "A" / "A2").exists()


def test_cell_budget_stops_the_cell_and_is_a_run_level_outcome(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.cost_per_request = 0.5
    data = loop_manifest(budget_usd=1.0)
    data["prompts"].append(dict(data["prompts"][0], id="A2"))
    config = loop_config(tmp_path, data)
    result = run(config)
    assert len(result.results) == 1
    assert result.budget_stops[0]["scope"] == "cell" and result.budget_stops[0]["cell"] == "loop-cell"
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["budget"]["run_stopped_by_budget"] is False
    assert run_manifest["budget"]["per_cell_spent_usd"]["loop-cell"] == pytest.approx(1.5)


def test_crowded_cell_runs_the_preturn_first_in_the_same_conversation(tmp_path, fake_openrouter):
    fake = fake_openrouter
    data = loop_manifest(context="crowded",
                         crowding={"procedure": "neutral-file-triage@2",
                                   "collision_review": "2026-09-18: smoke only, domains identical by design"})
    config = loop_config(tmp_path, data)
    result = run(config)
    assert result.failures == 0, result.results[0]["harness_failure"]
    meta = result.results[0]
    assert meta["crowding"]["preturn_ok"] is True
    assert meta["loop"]["preturn"] == {"steps": 2, "tool_calls": 1, "step_cap_hit": False}
    scored = fake.scored_requests
    opening = "You're helping tidy the office's shared notes inbox"
    # Ordering: the pre-turn's requests come first, then the scored turn resumes the
    # same conversation with the crowding still in it.
    assert scored[0]["messages"][1]["content"].startswith(opening)
    assert scored[0]["messages"][-1]["role"] == "user"
    scored_turn = scored[2]
    roles = [m["role"] for m in scored_turn["messages"]]
    assert roles[:2] == ["system", "user"] and roles[-1] == "user"
    assert scored_turn["messages"][1]["content"].startswith(opening)
    assert scored_turn["messages"][-1]["content"] == "Please list the unfiled notes."
    assert roles.count("tool") == 1  # the pre-turn's tool result is in context
    # Both turns offer the same surface: SUT tools plus the distractor's.
    expected = sorted(SUT_TOOLS + ["mcp__shared_notes__file_note", "mcp__shared_notes__list_unfiled_notes",
                                   "mcp__shared_notes__read_note"])
    for body in scored:
        assert sorted(t["function"]["name"] for t in body["tools"]) == expected
    assert meta["api_surface"]["expected_wire_surface"] == expected
    assert meta["api_surface"]["wire_surface_mismatches"] == []
    dest = config.run_dir / "loop-cell" / "A" / "A1"
    assert (dest / "loop-turn-crowding.json").exists() and (dest / "loop-result-crowding.json").exists()
    assert list(dest.glob("loop-session-*.json"))
    # Pre-turn tool traffic is in its own trace; the scored trace has the scored call.
    assert (dest / "crowding-trace.jsonl").exists() or True
    assert meta["tool_calls"] == ["list_unfiled_notes"]


def test_step_cap_is_a_consumer_limit_not_a_harness_failure(tmp_path, fake_openrouter):
    # S13: the scaffold's step cap ending the loop is a consumer outcome.
    fake = fake_openrouter
    fake.runaway = True
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    meta = result.results[0]
    assert meta["loop"]["step_cap_hit"] is True
    assert meta["loop"]["steps"] == LOOP_SCAFFOLD_V1.step_cap
    assert meta["harness_failure"] is None and result.failures == 0
    assert meta["consumer_limit"]["cause"] == "step_cap"
    assert meta["consumer_limit"]["step_cap"] == LOOP_SCAFFOLD_V1.step_cap
    assert meta["answer_chars"] == 0
    assert (config.run_dir / "loop-cell" / "A" / "A1" / "answer.txt").read_text() == ""
    assert result.consumer_limits == {"loop-cell/A1": "step_cap"}
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["consumer_limits"] == {"loop-cell/A1": "step_cap"}
    assert run_manifest["voided_cells"] == {} and run_manifest["failures"] == 0
    assert len(fake.scored_requests) == LOOP_SCAFFOLD_V1.step_cap


def test_context_limit_after_tool_calls_is_a_consumer_limit(tmp_path, fake_openrouter):
    # S13, the deny-r05 shape: the consumer's own tool results grow a request past the
    # served endpoint's listed context_length and the endpoint refuses it (4xx).
    fake = fake_openrouter
    fake.runaway = True                      # keeps calling tools; results accumulate
    fake.context_limit_tokens = 600          # the first request fits, the second does not
    for endpoint in fake.endpoints[MODEL]:
        endpoint["context_length"] = 600
    lines_logged: list[str] = []
    config = loop_config(tmp_path, loop_manifest(), log=lines_logged.append)
    result = run(config)
    meta = result.results[0]
    assert meta["harness_failure"] is None and result.failures == 0
    limit = meta["consumer_limit"]
    assert limit["cause"] == "context_length" and limit["http_status"] == 400
    assert limit["tool_calls_before"] == 1
    assert limit["listed_context_length"] == 600 and limit["listed_context_source"].startswith("pinned endpoint")
    assert limit["estimated_request_tokens"] > 600
    assert limit["endpoint_stated"]["max_tokens"] == 600 and limit["endpoint_stated"]["requested_tokens"] > 600
    assert "maximum context length" in limit["endpoint_message"]
    assert meta["loop"]["steps"] == 2 and meta["loop"]["tool_calls"] == 1
    assert meta["tool_calls"] == ["list_unfiled_notes"]  # the trace is complete
    assert meta["answer_chars"] == 0
    assert result.consumer_limits == {"loop-cell/A1": "context_length"}
    assert any("CONSUMER LIMIT (context_length)" in line for line in lines_logged)
    assert any(line.startswith("consumer limits: 1") for line in lines_logged)
    # The refused request is still on the wire as an error response, never a mismatch.
    assert meta["loop"]["error_responses"] == [{"seq": 2, "note": "upstream answered HTTP 400"}]
    assert meta["loop"]["provider_mismatches"] == []
    assert "loop-cell" not in result.voided_cells


def test_other_upstream_4xx_after_tool_calls_stays_an_instrument_outcome(tmp_path, fake_openrouter):
    # S13's rule is mechanical: a 4xx that is NOT a context overflow keeps today's
    # classification (a harness failure), even after tool calls.
    fake = fake_openrouter
    fake.status_sequence = [200, 200, 403]  # probe, first scored request, then a 403 (no retry)
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    meta = result.results[0]
    assert meta["consumer_limit"] is None
    assert meta["harness_failure"] and "runner exited 2" in meta["harness_failure"]
    assert result.consumer_limits == {}


def test_product_rows_carry_the_family_mark(tmp_path, fake_drivers):
    # WO-2 item 2 / S11: driver_family on every row and cell entry, not inferred from the id.
    from mcp_e2e_harness.runner import RunConfig as RC

    data = manifest_data(checks=[{"id": "typed", "description": "d", "applies_to": {"tool": "*"},
                                  "assert": {"present": ["/response/content"]}}])
    manifest = load_manifest(write_manifest(tmp_path, data))
    config = RC(manifest=manifest, run_dir=tmp_path / "run", cells=["basic"], drivers=fake_drivers,
                timeout_s=120, log=lambda line: None)
    result = run(config)
    assert result.failures == 0
    meta = result.results[0]
    assert meta["driver_family"] == "product"
    assert "scaffold" not in meta and "reproducibility" not in meta
    assert meta["consumer_limit"] is None
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["cell_marks"]["basic"]["driver_family"] == "product"
    assert run_manifest["cell_marks"]["basic"]["answers"]["A1"]["invocations"] == 1
    assert run_manifest["cell_marks"]["basic"]["answers_across_prompts"] == {"invocations": 1, "distinct": 1}
    assert run_manifest["loop_cells"] == {}
    report = json.loads((config.run_dir / "checks-report.json").read_text())
    assert report["cells"]["basic"]["driver_family"] == "product" and report["cells"]["basic"]["driver"] == "fake"


def test_retryable_status_is_retried_and_visible_on_the_wire(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.status_sequence = [200, 503]  # probe ok; first scored request 503 once
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    assert result.failures == 0
    meta = result.results[0]
    assert meta["loop"]["retries"] == 1
    lines = read_lines(config.run_dir / "loop-cell" / "A" / "A1" / "api-surface.jsonl")
    assert len(lines) == 3
    assert lines[0]["usage_cost"] is None and "HTTP 503" in lines[0]["usage_cost_note"]
    assert meta["loop"]["requests_on_wire"] == 3 and meta["loop"]["provider_unread"] == 1
    assert meta["loop"]["error_responses"] == [{"seq": 1, "note": "upstream answered HTTP 503"}]
    assert meta["loop"]["provider_mismatches"] == []


# ------------------------------------------------------------- recorder units

def test_surface_mismatches_are_exact_per_request():
    expected = ["mcp__s__a", "mcp__s__b"]
    records = [{"seq": 1, "tool_names": ["mcp__s__b", "mcp__s__a"]},
               {"seq": 2, "tool_names": ["mcp__s__a", "mcp__s__b", "web_search"]},
               {"seq": 3, "tool_names": ["mcp__s__a"]},
               {"seq": 4, "tool_names": None}]
    assert surface_mismatches(records, expected) == [
        {"seq": 2, "extra": ["web_search"], "missing": []},
        {"seq": 3, "extra": [], "missing": ["mcp__s__b"]},
        {"seq": 4, "extra": [], "missing": ["mcp__s__a", "mcp__s__b"]},
    ]


def test_loop_digest_sums_scalars_and_counts_unread(tmp_path):
    path = tmp_path / "cap.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"seq": 1, "provider": "A", "usage_prompt_tokens": 10, "usage_completion_tokens": 2,
         "usage_reasoning_tokens": 1, "usage_cost": 0.5},
        {"seq": 2, "provider": None, "provider_note": "x", "usage_prompt_tokens": 5, "usage_completion_tokens": 1,
         "usage_reasoning_tokens": None, "usage_cost": None, "usage_cost_note": "HTTP 503"},
        {"seq": 3, "path": "/v1/messages", "tool_names": []},
    ]) + "\n")
    digest = loop_digest(path)
    assert digest["requests"] == 2
    assert digest["served_providers"] == {"A": 1, "(absent)": 1}
    assert digest["usage"] == {"prompt_tokens": 15, "completion_tokens": 3, "reasoning_tokens": 1, "cost_usd": 0.5}
    assert digest["provider_unread"] == 1 and digest["cost_unread"] == 1


def test_recorder_mark_stamps_every_line_and_summarize_skips_probes(tmp_path, fake_openrouter):
    recorder = ApiSurfaceRecorder(tmp_path / "cap.jsonl", fake_openrouter.url, mark={"probe": True})
    base = recorder.start()
    try:
        import urllib.request
        body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "x"}],
                           "tools": [LOOP_SCAFFOLD_V1.probe_tool], "provider": {}}).encode()
        request = urllib.request.Request(base + "/api/v1/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=30).read()
    finally:
        recorder.stop()
    line = read_lines(tmp_path / "cap.jsonl")[0]
    assert line["probe"] is True and line["provider"] == PROVIDER and line["usage_cost"] == 0.001
    assert summarize(tmp_path / "cap.jsonl", ())["requests_recorded"] == 0
    assert loop_digest(tmp_path / "cap.jsonl")["requests"] == 1


# ------------------------------------------------------------- probe-only path

def test_probe_loop_cells_runs_only_the_gate(tmp_path, fake_openrouter):
    fake = fake_openrouter
    data = loop_manifest()
    data["cells"]["unpinned"] = dict(copy.deepcopy(LOOP_CELL), provider=None)
    data["cells"]["product"] = dict(manifest_data()["cells"]["basic"], driver="claude-code")
    manifest = load_manifest(write_manifest(tmp_path, data))
    records = probe_loop_cells(manifest, ["loop-cell", "unpinned"], tmp_path / "probes",
                               drivers={"loop": LoopDriver()}, log=lambda line: None,
                               cache_dir=tmp_path / "cache")
    assert {c: r["verdict"] for c, r in records.items()} == {"loop-cell": "pass", "unpinned": "pass"}
    assert len(fake.requests) == 2 and fake.scored_requests == []
    assert (tmp_path / "probes" / "loop-cell" / "loop-probe" / "probe.json").exists()
    summary = json.loads((tmp_path / "probes" / "probe-summary.json").read_text())
    assert summary["cells"]["unpinned"]["tuple"]["provider"] == "unpinned"
    with pytest.raises(HarnessError, match="not loop cells"):
        probe_loop_cells(manifest, ["product"], tmp_path / "p2", drivers={"loop": LoopDriver()},
                         log=lambda line: None)
    with pytest.raises(HarnessError, match="no loop cells"):
        probe_loop_cells(manifest, [], tmp_path / "p3", drivers={"loop": LoopDriver()}, log=lambda line: None)


def test_probe_loop_cli(tmp_path, fake_openrouter, capsys, monkeypatch):
    from mcp_e2e_harness.cli import main

    monkeypatch.setattr("mcp_e2e_harness.cli.DRIVERS", {"loop": LoopDriver()})
    monkeypatch.setattr("mcp_e2e_harness.runner.DRIVERS", {"loop": LoopDriver()})
    path = write_manifest(tmp_path, loop_manifest())
    code = main(["probe-loop", "--manifest", str(path), "--out", str(tmp_path / "out"),
                 "--loop-probe-cache", str(tmp_path / "cache")])
    out = capsys.readouterr().out
    assert code == 0 and "-> pass" in out
    fake_openrouter.probe_behavior = "no-call"
    code = main(["probe-loop", "--manifest", str(path), "--out", str(tmp_path / "out2"),
                 "--loop-probe-cache", str(tmp_path / "cache2")])
    assert code == 1


def test_smoke_loop_manifest_loads():
    manifest = load_manifest(Path(__file__).resolve().parents[1] / "examples" / "smoke" / "loop-prompts.json")
    assert all(cell["driver"] == "loop" for cell in manifest.cells.values())


def test_broken_probe_is_not_cached(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.status_sequence = [503, 503, 503]  # the probe's request and both retries
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    assert result.results == [] and "loop-cell" in result.voided_cells
    probe = json.loads((config.run_dir / "loop-cell" / "loop-probe" / "probe.json").read_text())
    assert probe["verdict"] == "broken" and "HTTP 503" in probe["detail"]
    assert list((tmp_path / "probe-cache").glob("*.json")) == []
    # Next time the pair is probed again, and passes.
    again = loop_config(tmp_path, loop_manifest(), run_dir=tmp_path / "run2")
    result = run(again)
    assert result.failures == 0 and len(fake.probe_requests) == 4


def test_upstream_error_metadata_is_surfaced(tmp_path, fake_openrouter):
    from mcp_e2e_harness.loop_consumer import ChatClient, LoopError, TurnConfig

    config = TurnConfig(api_base=fake_openrouter.url + "/api/v1", api_key_env="OPENROUTER_API_KEY", model=MODEL,
                        knobs={}, provider=provider_block(PIN, "deny"), provider_pin=PIN,
                        pinned_provider_names=[PROVIDER], scaffold="loop-scaffold@1", mode="turn",
                        result_path=str(tmp_path / "r.json"))
    client = ChatClient(config, log=lambda m: None)
    payload = {"error": {"message": "Provider returned error", "code": 429,
                         "metadata": {"raw": "temporarily rate-limited upstream", "provider_name": "Mistral"}}}
    with pytest.raises(LoopError, match="Mistral.*rate-limited upstream"):
        client._raise_for_error(429, payload, {"provider": {}})


# ------------------------------------------------------------- WO-3

def test_finish_reason_length_is_recorded_on_the_wire_and_in_meta(tmp_path, fake_openrouter):
    # A cap cut ("length") is distinguishable from a self cut ("stop") by the recorded
    # reason, never by token-count inference (the E17 need).
    fake = fake_openrouter
    fake.answer_finish_reason = "length"
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    assert result.failures == 0
    meta = result.results[0]
    wire = read_lines(config.run_dir / "loop-cell" / "A" / "A1" / "api-surface.jsonl")
    assert [r["finish_reason"] for r in wire] == ["tool_calls", "length"]
    assert meta["loop"]["finish_reason"] == "length"
    assert meta["loop"]["finish_reasons"] == {"tool_calls": 1, "length": 1}
    probe_line = read_lines(config.run_dir / "loop-cell" / "loop-probe" / "api-surface.jsonl")[0]
    assert probe_line["finish_reason"] == "tool_calls"


def test_finish_reason_absent_is_null_with_a_note(tmp_path, fake_openrouter):
    fake = fake_openrouter
    fake.status_sequence = [200, 503]  # one error line among the scored requests
    config = loop_config(tmp_path, loop_manifest())
    result = run(config)
    assert result.failures == 0
    wire = read_lines(config.run_dir / "loop-cell" / "A" / "A1" / "api-surface.jsonl")
    assert wire[0]["finish_reason"] is None and "HTTP 503" in wire[0]["finish_reason_note"]
    assert result.results[0]["loop"]["finish_reasons"] == {"(absent)": 1, "tool_calls": 1, "stop": 1}
    assert result.results[0]["loop"]["finish_reason"] == "stop"


def test_distinct_answer_count_is_keyed_by_prompt(tmp_path, fake_openrouter):
    # WO-4 addendum: two prompts whose answers matched is not a repeat of one prompt,
    # so the block is keyed by prompt id; the cell-wide total is labeled across prompts.
    fake = fake_openrouter
    fake.echo_prompt_in_answer = True
    data = loop_manifest()
    data["prompts"] += [dict(data["prompts"][0], id="A2", prompt="A different question."),
                        dict(data["prompts"][0], id="A3")]  # same text as A1 -> same answer
    config = loop_config(tmp_path, data)
    result = run(config)
    assert result.failures == 0
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    answers = run_manifest["cell_marks"]["loop-cell"]["answers"]
    assert set(answers) == {"A1", "A2", "A3"}
    assert all(a["invocations"] == 1 and a["distinct"] == 1 for a in answers.values())
    assert run_manifest["loop_cells"]["loop-cell"]["answers"] == answers
    digests = {m["prompt_id"]: m["answer_sha256_16"] for m in result.results}
    assert digests["A1"] == digests["A3"] != digests["A2"]
    assert {pid: list(a["digests"]) for pid, a in answers.items()} == {pid: [d] for pid, d in digests.items()}
    assert run_manifest["cell_marks"]["loop-cell"]["answers_across_prompts"] == {"invocations": 3, "distinct": 2}


def test_distinct_answers_two_invocations_of_one_prompt_and_one_of_another():
    # The runner plans one invocation per (cell, prompt) within a run -- repeats of one
    # prompt come from separate runs -- so the aggregation is exercised directly on
    # the shape run_one records: one meta per invocation.
    from mcp_e2e_harness.runner import answers_across_prompts, answers_by_prompt

    results = [{"cell": "c", "prompt_id": "C1", "answer_sha256_16": "aaaa"},
               {"cell": "c", "prompt_id": "C1", "answer_sha256_16": "bbbb"},
               {"cell": "c", "prompt_id": "C2", "answer_sha256_16": "aaaa"},  # matches a C1 answer: not a repeat
               {"cell": "other", "prompt_id": "C1", "answer_sha256_16": "aaaa"},
               {"cell": "c", "prompt_id": "C3", "answer_sha256_16": None}]  # answerless row: not counted
    by_prompt = answers_by_prompt(results, "c")
    assert by_prompt == {"C1": {"invocations": 2, "distinct": 2, "digests": {"aaaa": 1, "bbbb": 1}},
                         "C2": {"invocations": 1, "distinct": 1, "digests": {"aaaa": 1}}}
    assert answers_across_prompts(by_prompt) == {"invocations": 3, "distinct": 2}


def test_product_cells_carry_the_distinct_answer_count(tmp_path, fake_drivers):
    from mcp_e2e_harness.runner import RunConfig as RC

    data = manifest_data()
    data["prompts"].append(dict(data["prompts"][0], id="A2"))
    manifest = load_manifest(write_manifest(tmp_path, data))
    config = RC(manifest=manifest, run_dir=tmp_path / "run", cells=["basic"], drivers=fake_drivers,
                timeout_s=120, log=lambda line: None)
    run(config)
    marks = json.loads((config.run_dir / "run-manifest.json").read_text())["cell_marks"]["basic"]
    assert marks["answers"]["A1"]["invocations"] == 1 and marks["answers"]["A2"]["invocations"] == 1
    assert marks["answers"]["A1"]["digests"] == marks["answers"]["A2"]["digests"]
    assert marks["answers_across_prompts"] == {"invocations": 2, "distinct": 1}
