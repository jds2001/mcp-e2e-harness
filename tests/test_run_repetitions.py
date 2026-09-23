"""Repeated grid execution, invocation paths, counts, budgets, and environment."""
from __future__ import annotations

import json
from collections import Counter

import pytest
from conftest import FakeDriver, claude_cell_data, manifest_data, write_manifest
from scenarios.loop_config import loop_config, loop_manifest
from scenarios.repetitions import experiment

from mcp_e2e_harness.cli import main
from mcp_e2e_harness.manifest import load_manifest
from mcp_e2e_harness.runner import (
    HarnessError,
    RunConfig,
    answers_by_prompt,
    meta_relative_path,
    row_relative_path,
    run,
)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc"])
def test_cli_rejects_bad_repeats_before_loading_manifest(value):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--manifest", "/missing", "--repeats", value])
    assert exc.value.code == 2


@pytest.mark.parametrize("repeats", [None, 1, 3])
def test_cli_repeat_layout_and_dry_count(tmp_path, capsys, repeats):
    path = write_manifest(tmp_path, claude_cell_data())
    argv = ["run", "--manifest", str(path), "--run-dir", str(tmp_path / "run"), "--dry-run"]
    if repeats is not None:
        argv += ["--repeats", str(repeats)]
    assert main(argv) == 0
    assert f"invocations: {repeats or 1} " in capsys.readouterr().out
    manifest = json.loads((tmp_path / "run/run-manifest.json").read_text())
    assert manifest["selection"]["repeats"] == repeats
    for index, meta in enumerate(manifest["results"], 1):
        assert meta["repetition"] == (index if repeats is not None else None)
        path = tmp_path / "run/basic/A/A1"
        if repeats is not None:
            path /= f"r{index:02d}"
        assert json.loads((path / "meta.json").read_text()) == meta


def test_repetition_width_and_programmatic_validation(tmp_path):
    config = RunConfig(load_manifest(write_manifest(tmp_path, manifest_data())), tmp_path / "run", ["basic"])
    config.repeats = 100
    assert str(row_relative_path(config, "c", "A", "A1", 1)) == "c/A/A1/r001"
    for bad in (0, -1, True, 1.5):
        config.repeats = bad
        with pytest.raises(HarnessError, match="repeats"):
            run(config)
    assert not config.run_dir.exists()


def test_answers_count_attempted_and_only_nonempty_digests():
    rows = [{"cell": "c", "prompt_id": "A", "answer_chars": n, "answer_sha256_16": digest}
            for n, digest in [(1, "a"), (0, "empty"), (1, "a"), (1, "b")]]
    assert answers_by_prompt(rows, "c") == {"A": {
        "invocations": 4, "answered": 3, "distinct": 2, "digests": {"a": 2, "b": 1}}}
    assert answers_by_prompt([rows[1]], "c")["A"] == {
        "invocations": 1, "answered": 0, "distinct": 0, "digests": {}}


@pytest.mark.parametrize("answerless", [False, True])
def test_fake_acceptance_repeats(tmp_path, answerless):
    config, result = experiment(tmp_path, repeats=3, answerless=answerless)
    assert result.failures == 0
    assert [(m["cell"], m["repetition"]) for m in result.results] == [
        (cell, repetition) for repetition in (1, 2, 3) for cell in ("control", "treatment")]
    assert len({m["cwd"] for m in result.results}) == 6
    assert len({m["cell_id"] for m in result.results}) == 2
    pids = []
    setup_pids = []
    for meta in result.results:
        path = config.run_dir / meta_relative_path(config, meta)
        proxy = json.loads((path / "proxy-meta.json").read_text())
        pids.append(proxy["server_spawn"]["pid"])
        assert meta["env"] == {"WO6_ARM": meta["cell"]}
        assert meta["setup"][0]["response"]["content"][0]["text"] == meta["cell"]
        setup_pids.append(meta["setup"][0]["response"]["structuredContent"]["pid"])
        trace = json.loads((path / "trace.jsonl").read_text().splitlines()[0])
        assert trace["response"]["structuredContent"]["pid"] == pids[-1]
    assert len(set(pids)) == len(set(setup_pids)) == 6
    assert not set(pids) & set(setup_pids)
    assert len(list(config.probe_cache_dir.glob("*.json"))) == 1
    manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert manifest["cell_gates"]["treatment"]["cached"] is True
    progress = (tmp_path / "progress.txt").read_text().splitlines()
    assert [line for line in progress if line.startswith("  ->")] == [
        f"  -> {cell}/A1 r{repetition:02d}/3"
        for repetition in (1, 2, 3) for cell in ("control", "treatment")]
    assert sum("calibration probe ->" in line for line in progress) == 2
    for estimate in manifest["pre_run"]["loop"]["cells"].values():
        assert estimate["invocations"] == 3
        assert estimate["estimate"]["usd_total"] == pytest.approx(estimate["estimate"]["usd"] * 3)

    answers = manifest["cell_marks"]["control"]["answers"]["A1"]
    assert (answers["invocations"], answers["answered"], answers["distinct"]) == (
        3, 2 if answerless else 3, 1 if answerless else 2)
    assert answers["digests"] == dict(Counter(m["answer_sha256_16"] for m in result.results
                                            if m["cell"] == "control" and m["answer_chars"]))
    report = result.checks_report[0]
    assert report["outcome"] == "fail" and report["matched"] == 6
    assert report["cells"]["control"]["outcome"] == "pass"
    assert report["cells"]["treatment"]["outcome"] == "fail"
    assert {f["repetition"] for f in report["failures"]} == {1, 2, 3}
    assert all(f["cell"] == "treatment" and f["prompt_id"] == "A1" for f in report["failures"])


def test_cell_cap_skips_remaining_prompts_and_repetitions(tmp_path, fake_openrouter):  # noqa: F811
    fake_openrouter.cost_per_request = 0.5
    data = loop_manifest(budget_usd=1.0)
    data["prompts"].append(dict(data["prompts"][0], id="A2"))
    config = loop_config(tmp_path, data, repeats=3)
    result = run(config)
    assert len(result.results) == 1
    assert result.budget_stops[0]["skipped_in_cell"] == 5
    assert len(result.budget_stops) == 1


def test_repeated_zero_trace_rows_are_marked(tmp_path):
    # Product-family rows must use repetition paths for post-run liveness marking.
    data = manifest_data()
    data["cells"]["basic"]["merge_gating"] = False
    data["prompts"][0]["prompt"] = "do not call any tools"
    config = RunConfig(load_manifest(write_manifest(tmp_path, data)), tmp_path / "run", ["basic"],
                       repeats=2, drivers={"fake": FakeDriver()}, log=lambda _: None)
    result = run(config)
    if not result.zero_trace_cells:
        pytest.fail("fixture did not produce zero trace rows")
    for meta in result.results:
        row = json.loads((config.run_dir / meta_relative_path(config, meta) / "meta.json").read_text())
        assert "cell_void" in row


def test_repeated_product_crowding_and_measurements(tmp_path):
    data = manifest_data(measurements=[{
        "name": "coverage", "measure": "answer-coverage@1", "applies_to": {"tool": "*"},
        "reference": "/response/content/0/text", "floor": 16}])
    data["cells"]["basic"].update(context="crowded", crowding={
        "procedure": "neutral-file-triage@2", "collision_review": "disjoint notes server"})
    config = RunConfig(load_manifest(write_manifest(tmp_path, data)), tmp_path / "run", ["basic"],
                       repeats=2, drivers={"fake": FakeDriver()}, log=lambda _: None)
    result = run(config)
    assert result.failures == 0
    for meta in result.results:
        path = config.run_dir / meta_relative_path(config, meta)
        assert json.loads((path / "crowding.json").read_text())["exit_status"] == 0
        assert meta["crowding"]["preturn_ok"] is True
        assert len(meta["measurements"]["coverage"]) == 1
    manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert manifest["cell_marks"]["basic"]["answers"]["A1"]["answered"] == 2
    assert manifest["measurements"]["coverage"]["rows"] == 2


def test_repeated_run_cap_stops_future_passes(tmp_path, fake_openrouter):  # noqa: F811
    fake_openrouter.cost_per_request = 0.5
    config = loop_config(tmp_path, loop_manifest(), repeats=3, budget_usd=1.0)
    result = run(config)
    assert len(result.results) == 1
    assert len(result.budget_stops) == 1
    assert result.budget_stops[0]["scope"] == "run"
    assert result.budget_stops[0]["skipped_in_cell"] == 2


def test_cell_env_overrides_missing_transport_secret_and_records_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("WO6_MISSING_SECRET", raising=False)
    monkeypatch.setenv("WO6_PRESENT_SECRET", "test-only-secret-012345")
    data = manifest_data()
    data["server"]["transport"]["env"] = {"OVERRIDE": {"$secret": "WO6_MISSING_SECRET"}}
    data["cells"]["basic"]["env"] = {"OVERRIDE": "cell", "TOKEN": {"$secret": "WO6_PRESENT_SECRET"}}
    config = RunConfig(load_manifest(write_manifest(tmp_path, data)), tmp_path / "run", ["basic"],
                       drivers={"fake": FakeDriver()}, log=lambda _: None)
    result = run(config)
    assert result.failures == 0
    assert result.results[0]["env"] == {"OVERRIDE": "cell", "TOKEN": None}
    server = json.loads((config.run_dir / "basic/A/A1/server-config.json").read_text())
    assert server["env"]["OVERRIDE"] == "cell"
    assert "test-only-secret-012345" not in json.dumps(result.results)
