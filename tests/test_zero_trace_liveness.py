"""Evidence required for zero-trace null-final liveness exemptions."""
from __future__ import annotations

import json

import pytest
from scenarios.zero_trace import experiment

from mcp_e2e_harness.drivers.loop import LoopDriver
from mcp_e2e_harness.runner import meta_relative_path, zero_trace_cells


@pytest.mark.parametrize("kind", ["loop", "mismatch", "product"])
def test_zero_trace_acceptance_rows(tmp_path, kind):
    config, result = experiment(tmp_path, kind)
    meta = result.results[0]
    row = config.run_dir / meta_relative_path(config, meta)
    assert meta["trace_records"] == meta["answer_chars"] == 0
    assert meta["answer_sha256_16"] is None
    assert (row / "answer.txt").read_bytes() == b""
    assert meta["consumer_limit"]["cause"] == "null_final_content"
    evidence = meta["zero_trace_liveness"]
    assert evidence["spawn_check_passed"] is True
    manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert json.loads((row / "meta.json").read_text()) == manifest["results"][0] == meta
    if kind == "loop":
        assert evidence["exempted"] is True
        assert evidence["wire_surface_exact"] is True
        assert evidence["final_response_observed"] is True
        assert evidence["wire_requests"] == 1
        assert evidence["final_response"] == {"seq": 1, "http_status": 200, "finish_reason": "stop"}
        assert meta["consumer_limit"]["reasoning_tail_json"] is True
        assert meta["consumer_limit"]["reasoning_tail_matches_tools"] == ["mcp__notes_sut__file_note"]
        assert meta["loop"]["steps"] == 1 and meta["loop"]["retries"] == 0
        assert meta["harness_failure"] is None and result.failures == 0
        assert manifest["consumer_limits"] == {"loop-cell/A1": "null_final_content"}
        assert not list(config.run_dir.rglob("CELL-VOID.json"))
        wire = json.loads((row / "api-surface.jsonl").read_text())
        assert wire["http_status"] == 200 and wire["finish_reason"] == "stop"
    else:
        assert evidence["exempted"] is False and evidence["wire_surface_exact"] is False
        assert meta["harness_failure"] and meta["cell_void"]
        assert result.failures > 0 and manifest["consumer_limits"] == {}
        assert result.zero_trace_cells == [meta["cell"]]
        assert json.loads((config.run_dir / meta["cell"] / "CELL-VOID.json").read_text())["verdict"] == "BROKEN"
        if kind == "product":
            assert meta["exit_status"] == 0 and meta["driver_family"] == "product"
            assert evidence["wire_requests"] == 0 and evidence["final_response_observed"] is False
        else:
            assert evidence["final_response_observed"] is True


@pytest.mark.parametrize("fault, observation", [
    ("spawn_false", "spawn_check_passed"),
    ("spawn_missing", "spawn_check_passed"),
    ("surface", "wire_surface_exact"),
    ("wire_missing", "wire_surface_exact"),
    ("wire_malformed", "wire_surface_exact"),
    ("status_missing", "final_response_observed"),
    ("status_500", "final_response_observed"),
    ("finish_missing", "final_response_observed"),
    ("finish_empty", "final_response_observed"),
    ("incomplete", "final_response_observed"),
])
def test_each_missing_observation_leaves_row_broken(tmp_path, monkeypatch, fault, observation):
    original = LoopDriver.after_turn

    def damage_artifact(self, turn, dest, cell, api_path, cell_name=None):
        # Fault injection at the boundary between recording and row interpretation.
        extra = original(self, turn, dest, cell, api_path, cell_name)
        spawn = dest.parents[1] / "spawn-check.json"
        if fault == "spawn_missing":
            spawn.unlink()
        elif fault == "spawn_false":
            record = json.loads(spawn.read_text())
            record["ok"] = False
            spawn.write_text(json.dumps(record))
        elif fault == "wire_missing":
            api_path.unlink()
        elif fault == "wire_malformed":
            with api_path.open("a") as handle:
                handle.write("not a wire record\n")
        else:
            wire = json.loads(api_path.read_text())
            if fault == "surface":
                wire["tool_names"] = ["injected_extra"]
            elif fault == "status_missing":
                wire.pop("http_status")
            elif fault == "status_500":
                wire["http_status"] = 500
            elif fault == "finish_missing":
                wire.pop("finish_reason")
            elif fault == "finish_empty":
                wire["finish_reason"] = ""
            elif fault == "incomplete":
                wire["duration_ms"] = None
                wire["duration_note"] = "response did not complete"
            api_path.write_text(json.dumps(wire) + "\n")
        return extra

    monkeypatch.setattr(LoopDriver, "after_turn", damage_artifact)
    config, result = experiment(tmp_path)
    meta = result.results[0]
    assert meta["zero_trace_liveness"][observation] is False
    assert meta["zero_trace_liveness"]["exempted"] is False
    assert meta["harness_failure"] and meta["cell_void"]
    assert result.failures > 0 and result.consumer_limits == {}
    assert (config.run_dir / "loop-cell/CELL-VOID.json").exists()


def test_exemption_is_loop_only():
    valid = {"cell": "cell", "driver_family": "loop", "trace_records": 0,
             "consumer_limit": {"cause": "null_final_content"},
             "zero_trace_liveness": {"exempted": True}, "harness_failure": None}
    assert zero_trace_cells([valid], False) == []
    product = dict(valid, driver_family="product")
    assert zero_trace_cells([product], False) == ["cell"]


def test_valid_sibling_does_not_exempt_a_row_with_missing_evidence(tmp_path, monkeypatch):
    original = LoopDriver.after_turn

    def damage_second(self, turn, dest, cell, api_path, cell_name=None):
        extra = original(self, turn, dest, cell, api_path, cell_name)
        if dest.name == "r02":
            wire = json.loads(api_path.read_text())
            wire.pop("http_status")
            api_path.write_text(json.dumps(wire) + "\n")
        return extra

    monkeypatch.setattr(LoopDriver, "after_turn", damage_second)
    _, result = experiment(tmp_path, repeats=2)
    good, bad = result.results
    assert good["zero_trace_liveness"]["exempted"] is True
    assert good["harness_failure"] is None
    assert bad["zero_trace_liveness"]["exempted"] is False
    assert bad["harness_failure"] and result.failures > 0
    assert result.consumer_limits == {"loop-cell/A/A1/r01": "null_final_content"}
