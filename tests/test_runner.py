"""Run mechanics: planning, neutral cwd, and end-to-end runs with a fake driver.

The integration tests run the whole chain -- runner -> fake consumer subprocess ->
trace proxy -> distractor server (as SUT) -- so trace records, artifacts, checks, and
the liveness/hygiene built-ins are exercised over real process boundaries.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import manifest_data, write_manifest

from mcp_e2e_harness.manifest import load_manifest
from mcp_e2e_harness.runner import (
    HarnessError,
    RunConfig,
    make_neutral_cwd,
    plan_invocations,
    resolve_prompt,
    run,
)
from mcp_e2e_harness.secrets import MissingSecretError, SecretLeakError


def load(tmp_path, data) -> Manifest:  # noqa: F821
    return load_manifest(write_manifest(tmp_path, data))


def config_for(tmp_path, data, fake_drivers, **overrides) -> RunConfig:
    manifest = load(tmp_path, data)
    defaults = dict(manifest=manifest, run_dir=tmp_path / "run",
                    cells=list(manifest.cells), drivers=fake_drivers, timeout_s=120)
    defaults.update(overrides)
    return RunConfig(**defaults)


# ---------------------------------------------------------------- planning

def two_group_data() -> dict:
    data = manifest_data()
    data["prompts"].append({"id": "B1", "group": "B", "title": "t", "prompt": "p",
                            "sourcing": "derived", "pass": "p", "fail": "f"})
    data["cells"]["basic"]["groups"] = ["A", "B"]
    data["cells"]["narrow"] = dict(data["cells"]["basic"], groups=["A"])
    return data


def test_plan_honors_groups_and_allowlists(tmp_path):
    manifest = load(tmp_path, two_group_data())
    planned = plan_invocations(manifest, ["basic", "narrow"], None, None)
    assert [(p.entry["id"], p.cell_name) for p in planned] == [
        ("A1", "basic"), ("B1", "basic"), ("A1", "narrow")]


def test_explicit_prompt_request_runs_off_grid_and_is_marked(tmp_path):
    manifest = load(tmp_path, two_group_data())
    planned = plan_invocations(manifest, ["narrow"], None, {"B1"})
    assert [(p.entry["id"], p.outside_cell_groups) for p in planned] == [("B1", True)]


def test_group_filter(tmp_path):
    manifest = load(tmp_path, two_group_data())
    planned = plan_invocations(manifest, ["basic"], {"B"}, None)
    assert [p.entry["id"] for p in planned] == ["B1"]


# ---------------------------------------------------------------- neutral cwd

def test_neutral_cwd_refuses_project_context_above_it(tmp_path):
    for marker in (".git", "CLAUDE.md", "AGENTS.md"):
        base = tmp_path / marker.strip(".") / "scratch"
        base.mkdir(parents=True)
        if marker == ".git":
            (base.parent / marker).mkdir()
        else:
            (base.parent / marker).write_text("context")
        with pytest.raises(HarnessError, match="walking up"):
            make_neutral_cwd("t", base)


def test_neutral_cwd_is_empty_and_fresh(tmp_path):
    first = make_neutral_cwd("t", tmp_path)
    second = make_neutral_cwd("t", tmp_path)
    assert first != second
    assert list(first.iterdir()) == []


# ---------------------------------------------------------------- variants

def test_resolve_prompt_variant(tmp_path):
    entry = {"id": "X", "prompt": "base", "variants": {"single_step": "pre-resolved"}}
    assert resolve_prompt(entry, {"variant": "single_step"}) == "pre-resolved"
    assert resolve_prompt(entry, {}) == "base"
    with pytest.raises(HarnessError, match="variant"):
        resolve_prompt({"id": "X", "prompt": "base"}, {"variant": "single_step"})


# ---------------------------------------------------------------- preflight

def test_http_transport_is_refused_as_instrument_broken(tmp_path, fake_drivers):
    data = manifest_data()
    data["server"]["transport"] = {"type": "http", "url": "https://example.test/mcp"}
    config = config_for(tmp_path, data, fake_drivers)
    with pytest.raises(HarnessError, match="instrument-broken"):
        run(config)


def test_unknown_driver_is_refused_not_skipped(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["driver"] = "mystery-cli"
    config = config_for(tmp_path, data, fake_drivers)
    with pytest.raises(HarnessError, match="mystery-cli"):
        run(config)


def test_missing_secret_fails_once_before_anything_is_spent(tmp_path, fake_drivers):
    data = manifest_data()
    data["server"]["transport"]["env"] = {"KEY": {"$secret": "UNSET_VAR_FOR_TEST"}}
    config = config_for(tmp_path, data, fake_drivers)
    with pytest.raises(MissingSecretError, match="UNSET_VAR_FOR_TEST"):
        run(config)
    assert not (tmp_path / "run").exists() or not any((tmp_path / "run").iterdir())


def test_nonempty_run_dir_is_refused(tmp_path, fake_drivers):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "stale").write_text("old run")
    config = config_for(tmp_path, manifest_data(), fake_drivers, run_dir=run_dir, dry_run=True)
    with pytest.raises(HarnessError, match="not empty"):
        run(config)


def test_zero_invocations_selected_is_refused(tmp_path, fake_drivers):
    config = config_for(tmp_path, manifest_data(), fake_drivers, groups={"ZZZ"}, dry_run=True)
    with pytest.raises(HarnessError, match="zero invocations"):
        run(config)


# ---------------------------------------------------------------- dry run

def test_dry_run_writes_layout_and_calls_nothing(tmp_path, fake_drivers):
    config = config_for(tmp_path, manifest_data(), fake_drivers, dry_run=True)
    result = run(config)
    assert result.failures == 0
    meta = result.results[0]
    assert meta["harness_failure"] is None
    assert meta["trace_records"] == 0
    assert not result.zero_trace_cells  # a dry run's zero traces are not dead cells
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["dry_run"] is True
    assert run_manifest["manifest"]["sha256"] == config.manifest.sha256
    assert (config.run_dir / "basic" / "A" / "A1" / "mcp-config.json").exists()
    assert (config.run_dir / "basic" / "A" / "A1" / "server-config.json").exists()


# ---------------------------------------------------------------- live (fake) runs

def checks_for_tests() -> list[dict]:
    return [
        {"id": "content-typed", "description": "every response carries typed content",
         "applies_to": {"tool": "*"},
         "assert": {"present": ["/response/content/~each/type"]}},
        {"id": "never-fires", "applies_to": {"tool": "no_such_tool"},
         "assert": {"present": ["/response"]}},
        {"id": "forbid-ghost", "applies_to": {"tool": "*"},
         "assert": {"forbid_pattern": {"regex": "GHOST-MARKER"}}},
    ]


def test_end_to_end_records_trace_and_checks(tmp_path, fake_drivers):
    data = manifest_data(checks=checks_for_tests())
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.failures == 0

    meta = result.results[0]
    assert meta["harness_failure"] is None
    assert meta["trace_records"] >= 1
    assert meta["tool_calls"] == ["list_unfiled_notes"]
    assert meta["driver"]["cli_version"]

    dest = config.run_dir / "basic" / "A" / "A1"
    records = [json.loads(line) for line in (dest / "trace.jsonl").read_text().splitlines()]
    assert records[0]["tool"] == "list_unfiled_notes"
    answer = (dest / "answer.txt").read_text()
    assert "list_unfiled_notes returned" in answer

    surface = json.loads((dest / "available-tools.json").read_text())
    assert "list_unfiled_notes" in surface["exposed"]
    meta_json = json.loads((dest / "meta.json").read_text())
    assert meta_json["recorded_tool_surface"]["advertised"] == surface["advertised"]
    assert meta_json["criteria"]["pass"] == "lists notes"

    by_id = {c["id"]: c["outcome"] for c in result.checks_report}
    assert by_id == {"content-typed": "pass", "never-fires": "vacuous", "forbid-ghost": "pass"}
    assert (config.run_dir / "checks-report.json").exists()


def test_zero_trace_cell_is_broken_never_clean(tmp_path, fake_drivers):
    data = manifest_data()
    data["prompts"][0]["prompt"] = "Answer from memory; do not call any tools."
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.zero_trace_cells == ["basic"]
    assert result.failures == 1
    void = json.loads((config.run_dir / "basic" / "CELL-VOID.json").read_text())
    assert void["verdict"] == "BROKEN"
    stamped = json.loads((config.run_dir / "basic" / "A" / "A1" / "meta.json").read_text())
    assert "cell_void" in stamped


def test_setup_actions_run_server_side_and_are_recorded(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["setup"] = [
        {"tool": "file_note", "args": {"note_id": "n01", "folder": "archive"}}]
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.failures == 0
    setup = json.loads((config.run_dir / "basic" / "A" / "A1" / "setup.json").read_text())
    assert setup[0]["tool"] == "file_note"
    assert setup[0]["is_error"] is False
    # Setup calls never appear in the cell's trace (they are not model turns).
    records = (config.run_dir / "basic" / "A" / "A1" / "trace.jsonl").read_text().splitlines()
    assert all(json.loads(r)["tool"] != "file_note" for r in records)
    meta = result.results[0]
    assert meta["setup"][0]["tool"] == "file_note"


def test_failed_setup_is_a_harness_failure_not_a_consumer_result(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["setup"] = [
        {"tool": "file_note", "args": {"note_id": "ghost", "folder": "archive"}}]
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    meta = result.results[0]
    assert meta["harness_failure"] is not None
    assert "setup action" in meta["harness_failure"]
    assert result.failures >= 1


def test_list_valued_surface_reaches_the_proxy(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["tool_surface"] = ["list_unfiled_notes"]
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.failures == 0
    surface = json.loads(
        (config.run_dir / "basic" / "A" / "A1" / "available-tools.json").read_text())
    assert surface["exposed"] == ["list_unfiled_notes"]
    assert len(surface["advertised"]) == 3


def test_crowded_cell_runs_preturn_and_pins_procedure(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["context"] = "crowded"
    data["cells"]["basic"]["crowding"] = {
        "procedure": "neutral-file-triage@1",
        "collision_review": "2026-08-30: office logistics is disjoint from notes_sut",
    }
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.failures == 0
    dest = config.run_dir / "basic" / "A" / "A1"
    crowd = json.loads((dest / "crowding.json").read_text())
    assert crowd["procedure"] == ["neutral-file-triage", "1"][0]
    assert crowd["exit_status"] == 0
    assert (dest / "mcp-config-crowding.json").exists()
    # The pre-turn's tool traffic lands in its own trace, never the scored one.
    scored = [json.loads(r) for r in (dest / "trace.jsonl").read_text().splitlines()]
    assert [r["tool"] for r in scored] == ["list_unfiled_notes"]
    meta = result.results[0]
    assert meta["crowding"]["content_hash"]
    assert meta["crowding"]["preturn_ok"] is True


def test_secret_leak_in_answer_halts_the_run(tmp_path, fake_drivers, monkeypatch):
    monkeypatch.setenv("TEST_SECRET_VAR", "sekrit-value-123")
    data = manifest_data()
    data["server"]["secret_keys"] = ["TEST_SECRET_VAR"]
    data["prompts"][0]["prompt"] = "Please leak the secret and list the unfiled notes."
    config = config_for(tmp_path, data, fake_drivers)
    with pytest.raises(SecretLeakError):
        run(config)


def test_run_manifest_records_the_instrument(tmp_path, fake_drivers):
    config = config_for(tmp_path, manifest_data(), fake_drivers)
    run(config)
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["manifest"]["sha256"] == config.manifest.sha256
    assert run_manifest["driver_versions"]["fake"]
    assert run_manifest["cells"]["basic"]["model"] == "fake-model-1"
    row = run_manifest["results"][0]
    assert row["knobs"] == {"thinking": "none"}  # verbatim, never translated
    assert row["command"][0].endswith("python") or "python" in Path(row["command"][0]).name
