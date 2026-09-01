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
    probe_builtin_surface,
    resolve_prompt,
    run,
)
from mcp_e2e_harness.secrets import MissingSecretError, SecretLeakError


def load(tmp_path, data) -> Manifest:  # noqa: F821
    return load_manifest(write_manifest(tmp_path, data))


def config_for(tmp_path, data, fake_drivers, **overrides) -> RunConfig:
    manifest = load(tmp_path, data)
    defaults = dict(manifest=manifest, run_dir=tmp_path / "run",
                    cells=list(manifest.cells), drivers=fake_drivers, timeout_s=120,
                    log=lambda line: None)
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

def test_relative_run_dir_still_yields_absolute_config_paths(tmp_path, fake_drivers, monkeypatch):
    # The consumer runs in a neutral temp cwd, so every path handed to the driver must
    # survive a cwd change. Regression: a relative --run-dir made claude fail with
    # "MCP config file not found" resolved against the neutral cwd (2026-08-31).
    monkeypatch.chdir(tmp_path)
    manifest = load(tmp_path, manifest_data())
    config = RunConfig(manifest=manifest, run_dir=Path("relative-run"),
                       cells=list(manifest.cells), drivers=fake_drivers, dry_run=True)
    result = run(config)
    assert result.run_dir.is_absolute()
    config_path = Path(result.results[0]["command"][2])  # FakeDriver argv: [py, consumer, config]
    assert config_path.is_absolute()
    assert config_path.exists()


# ------------------------------------- spawn liveness, S8 (DR-1) + operator log, S9

def test_dead_sut_breaks_the_cell_before_any_model_turn(tmp_path, fake_drivers):
    # DR-1's shape: the server command dies at spawn (unknown procedure -> exit 2).
    data = manifest_data()
    data["server"]["transport"]["args"] = [
        "-m", "mcp_e2e_harness.distractor", "--procedure", "ghost@9"]
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.voided_cells and "spawn" in result.voided_cells["basic"]
    assert result.failures == 1
    # Zero model turns were spent: no invocation directory, no result rows.
    assert result.results == []
    assert not (config.run_dir / "basic" / "A").exists()
    void = json.loads((config.run_dir / "basic" / "CELL-VOID.json").read_text())
    assert void["verdict"] == "BROKEN"
    check = json.loads((config.run_dir / "basic" / "spawn-check.json").read_text())
    assert check["ok"] is False
    assert "ghost@9" in check["stderr_tail"]  # the server's own account is kept
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert "basic" in run_manifest["voided_cells"]


def test_spawn_check_runs_under_invocation_conditions_neutral_cwd(tmp_path, fake_drivers):
    # DR-1's mechanism was cwd-dependence: a check from the harness cwd would pass
    # while every real turn failed. The check must run from a neutral cwd.
    config = config_for(tmp_path, manifest_data(), fake_drivers)
    result = run(config)
    assert result.failures == 0
    check = json.loads((config.run_dir / "basic" / "spawn-check.json").read_text())
    assert check["ok"] is True
    assert check["advertised_tools"] == ["list_unfiled_notes", "read_note", "file_note"]
    cwd = Path(check["cwd"])
    assert cwd != Path.cwd()
    assert Path.cwd() not in cwd.parents


def test_zero_tool_server_breaks_at_spawn(tmp_path, fake_drivers):
    data = manifest_data()
    data["server"]["transport"]["args"] = [str(Path(__file__).parent / "fake_empty_server.py")]
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert "zero tools" in result.voided_cells["basic"]
    assert result.results == []


def test_unadvertised_surface_tool_breaks_at_spawn(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["tool_surface"] = ["read_note", "ghost_tool"]
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert "ghost_tool" in result.voided_cells["basic"]
    assert result.results == []


def test_dry_run_skips_the_spawn_check(tmp_path, fake_drivers):
    data = manifest_data()
    data["server"]["transport"]["args"] = [
        "-m", "mcp_e2e_harness.distractor", "--procedure", "ghost@9"]
    config = config_for(tmp_path, data, fake_drivers, dry_run=True)
    result = run(config)
    assert result.voided_cells == {}
    assert len(result.results) == 1


def test_runner_is_never_silent(tmp_path, fake_drivers):
    # S9: run dir named up front; cell/prompt starts, turn transitions, and
    # completions visible live; in-flight vs broken distinguishable.
    lines: list[str] = []
    config = config_for(tmp_path, manifest_data(), fake_drivers, log=lines.append)
    run(config)
    joined = "\n".join(lines)
    run_dir_at = next(i for i, line in enumerate(lines) if "run dir" in line)
    first_invocation_at = next(i for i, line in enumerate(lines) if "-> basic/A1" in line)
    assert run_dir_at < first_invocation_at  # named before anything can go wrong
    assert any("spawn check live" in line for line in lines)
    assert "scored turn" in joined
    assert any("trace record(s)" in line for line in lines)
    assert "harness failures: 0" in joined


def test_broken_spawn_is_surfaced_live(tmp_path, fake_drivers):
    lines: list[str] = []
    data = manifest_data()
    data["server"]["transport"]["args"] = [
        "-m", "mcp_e2e_harness.distractor", "--procedure", "ghost@9"]
    config = config_for(tmp_path, data, fake_drivers, log=lines.append)
    run(config)
    assert any("BROKEN at spawn" in line for line in lines)
    assert any("skipped without spending model turns" in line for line in lines)


# ------------------------------------------ API-boundary surface capture (Q2, wire)

def capture_drivers() -> dict:
    from conftest import FakeDriver

    class CapturingFakeDriver(FakeDriver):
        # The harness env var FAKE_API_BASE (set by the test) overrides the default
        # upstream; the non-empty default is what marks the driver capture-capable.
        api_base_env = "FAKE_API_BASE"
        api_default_upstream = "http://upstream-not-configured.invalid"

    return {"fake": CapturingFakeDriver()}


def test_capture_observes_the_wire_surface_on_a_scored_invocation(
        tmp_path, fake_api_upstream, monkeypatch):
    monkeypatch.setenv("FAKE_API_BASE", fake_api_upstream)
    config = config_for(tmp_path, manifest_data(), capture_drivers())
    result = run(config)
    assert result.failures == 0
    surface = result.results[0]["api_surface"]
    assert surface["requests_with_tools"] == 1
    assert "mcp__notes_sut__list_unfiled_notes" in surface["tool_names_union"]
    assert surface["disallowed_builtins_on_wire"] == []
    dest = config.run_dir / "basic" / "A" / "A1"
    record = json.loads((dest / "api-surface.jsonl").read_text().splitlines()[0])
    assert record["model"] == "fake-model-1"
    # A capture-capable gating driver is verified on the wire, not by the
    # calibration probe.
    assert result.driver_probes == {}
    assert not (config.run_dir / "driver-probe").exists()


def test_disallowed_builtin_on_the_wire_is_an_instrument_breach(
        tmp_path, fake_api_upstream, monkeypatch):
    monkeypatch.setenv("FAKE_API_BASE", fake_api_upstream)
    monkeypatch.setenv("FAKE_WIRE_BUILTIN", "1")
    config = config_for(tmp_path, manifest_data(), capture_drivers())
    result = run(config)
    assert result.failures >= 1
    meta = result.results[0]
    assert meta["api_surface"]["disallowed_builtins_on_wire"] == ["FakeWeb"]
    assert "not tool-attributable" in meta["harness_failure"]


def test_unrouted_capture_reads_as_unverified_never_as_clean(
        tmp_path, fake_api_upstream, monkeypatch):
    monkeypatch.setenv("FAKE_API_BASE", fake_api_upstream)
    monkeypatch.setenv("FAKE_IGNORE_API_BASE", "1")
    config = config_for(tmp_path, manifest_data(), capture_drivers())
    result = run(config)
    assert result.failures >= 1
    meta = result.results[0]
    assert meta["api_surface"]["requests_with_tools"] == 0
    assert "unverified" in meta["harness_failure"]


# ---------------------------------------------- egress canary (codex Q2 residual)

def test_egress_canary_passes_only_on_the_honest_failure_protocol(tmp_path, fake_drivers):
    from mcp_e2e_harness.runner import EGRESS_PASS, probe_egress

    record = probe_egress(fake_drivers["fake"], tmp_path / "probe")
    assert record["verdict"] == EGRESS_PASS
    assert "NO NETWORK ACCESS" in record["answer"]
    probe = json.loads((tmp_path / "probe" / "probe.json").read_text())
    assert probe["url"].startswith("https://")
    assert (tmp_path / "probe" / "runner-stdout.txt").exists()


def test_egress_canary_fails_on_any_reported_fetch(tmp_path, fake_drivers, monkeypatch):
    from mcp_e2e_harness.runner import probe_egress

    # A fetched result and a fabricated one are indistinguishable by design; both
    # fail into investigation, never into a pass.
    monkeypatch.setenv("FAKE_EGRESS_OPEN", "1")
    record = probe_egress(fake_drivers["fake"], tmp_path / "probe")
    assert record["verdict"] == "egress-not-verified-blocked"
    assert "200 OK" in record["detail"]


def test_egress_canary_refuses_driver_without_probe_model(tmp_path, fake_drivers):
    from conftest import FakeDriver

    from mcp_e2e_harness.runner import probe_egress

    class NoEgressDriver(FakeDriver):
        egress_probe_model = ""

    with pytest.raises(HarnessError, match="egress-probe model"):
        probe_egress(NoEgressDriver(), tmp_path / "probe")


# ---------------------------- codex-shaped driver mechanics, exercised via the fake

def two_prompt_data() -> dict:
    data = manifest_data()
    data["prompts"].append({"id": "A2", "group": "A", "title": "t2",
                            "prompt": "Please list the unfiled notes.", "sourcing": "derived",
                            "pass": "p", "fail": "f"})
    return data


def test_web_event_breach_breaks_the_cell_hard(tmp_path, fake_drivers, monkeypatch):
    # 50-drivers.md codex #5: a web event in the driver's own stream is an instrument
    # breach -- the row is not tool-attributable and the CELL is BROKEN, remaining
    # prompts skipped. The measured failure class includes a fabricated fetch result.
    from conftest import FakeDriver

    class WebEventFakeDriver(FakeDriver):
        web_event_markers = ("fake web event",)

    monkeypatch.setenv("FAKE_WEB_EVENT", "1")
    config = config_for(tmp_path, two_prompt_data(), {"fake": WebEventFakeDriver()})
    result = run(config)
    assert len(result.results) == 1  # A2 skipped after A1's breach
    meta = result.results[0]
    assert meta["web_activity_suspected"] == ["fake web event"]
    assert "not tool-attributable" in meta["harness_failure"]
    assert "basic" in result.voided_cells
    void = json.loads((config.run_dir / "basic" / "CELL-VOID.json").read_text())
    assert "attribution breach" in void["reason"]


def test_answer_can_travel_by_file_with_stdout_kept_as_event_stream(tmp_path, fake_drivers):
    from conftest import FAKE_CONSUMER, FakeDriver

    from mcp_e2e_harness.drivers.base import TurnSpec

    class FileAnswerFakeDriver(FakeDriver):
        def build_turn(self, ctx):
            answer_path = ctx.dest / "fake-answer.txt"
            argv = [FakeDriver.executable, str(FAKE_CONSUMER), str(ctx.mcp_config_path)]
            return TurnSpec(argv=argv, stdin_text=ctx.prompt, answer_from="file",
                            answer_path=answer_path,
                            env_overrides={"FAKE_ANSWER_FILE": str(answer_path)})

    data = manifest_data()
    data["cells"]["basic"]["merge_gating"] = False  # this fake's probe answer is file-bound
    config = config_for(tmp_path, data, {"fake": FileAnswerFakeDriver()})
    result = run(config)
    assert result.failures == 0
    dest = config.run_dir / "basic" / "A" / "A1"
    assert "list_unfiled_notes returned" in (dest / "answer.txt").read_text()
    # The driver's stdout (its event stream) is kept as its own artifact.
    assert "turn complete" in (dest / "runner-stdout.txt").read_text()
    meta = result.results[0]
    assert meta["env_overrides"]["FAKE_ANSWER_FILE"].endswith("fake-answer.txt")


def test_sanitizing_driver_gets_secrets_to_the_proxy_by_file_never_artifact(
        tmp_path, fake_drivers, monkeypatch):
    # 50-drivers.md codex #6: the driver strips the env of MCP servers it spawns, so
    # $secret resolution must ride the transient 0600 file. FAKE_SANITIZE_VARS makes
    # the fake consumer strip the var exactly as codex would.
    monkeypatch.setenv("INJECTED_SECRET_VAR", "sekrit-injection-value")
    monkeypatch.setenv("FAKE_SANITIZE_VARS", "INJECTED_SECRET_VAR")
    from conftest import FakeDriver

    class SanitizingFakeDriver(FakeDriver):
        sanitizes_mcp_env = True

    data = manifest_data()
    data["server"]["secret_keys"] = ["INJECTED_SECRET_VAR"]
    data["server"]["transport"]["env"] = {"INJECTED": {"$secret": "INJECTED_SECRET_VAR"}}
    config = config_for(tmp_path, data, {"fake": SanitizingFakeDriver()})
    result = run(config)
    assert result.failures == 0
    assert result.results[0]["trace_records"] >= 1  # the server came up WITH the secret
    mcp_config = (config.run_dir / "basic" / "A" / "A1" / "mcp-config.json").read_text()
    assert "--secrets-file" in mcp_config
    secrets_path = Path(json.loads(mcp_config)["mcpServers"]["notes_sut"]["args"][-1])
    assert not secrets_path.exists()  # deleted when the invocation ended
    assert str(config.run_dir.resolve()) not in str(secrets_path)
    # The value itself reached no artifact (the hygiene scan would have halted; check
    # the config bytes directly as well).
    assert "sekrit-injection-value" not in mcp_config


def test_without_the_secrets_file_the_sanitized_proxy_dies(tmp_path, fake_drivers, monkeypatch):
    # Control arm: same sanitized env, driver NOT declaring sanitizes_mcp_env -- the
    # proxy relies on inheritance, the fake consumer strips the var, and the secret
    # never arrives. Proves the file (not ambient env) delivered the secret above.
    monkeypatch.setenv("INJECTED_SECRET_VAR", "sekrit-injection-value")
    monkeypatch.setenv("FAKE_SANITIZE_VARS", "INJECTED_SECRET_VAR")
    data = manifest_data()
    data["server"]["secret_keys"] = ["INJECTED_SECRET_VAR"]
    data["server"]["transport"]["env"] = {"INJECTED": {"$secret": "INJECTED_SECRET_VAR"}}
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.failures >= 1


def test_crowded_cell_on_sessionless_driver_is_refused_at_preflight(tmp_path, fake_drivers):
    from conftest import FakeDriver

    class SessionlessFakeDriver(FakeDriver):
        supports_sessions = False

    data = manifest_data()
    data["cells"]["basic"]["context"] = "crowded"
    data["cells"]["basic"]["crowding"] = {"procedure": "neutral-file-triage@2",
                                          "collision_review": "dated attestation"}
    config = config_for(tmp_path, data, {"fake": SessionlessFakeDriver()})
    with pytest.raises(HarnessError, match="sessions"):
        run(config)


def test_missing_required_env_is_refused_at_preflight(tmp_path, fake_drivers, monkeypatch):
    from conftest import FakeDriver

    class KeyedFakeDriver(FakeDriver):
        required_env = ("UNSET_PROVIDER_KEY_VAR",)

    monkeypatch.delenv("UNSET_PROVIDER_KEY_VAR", raising=False)
    config = config_for(tmp_path, manifest_data(), {"fake": KeyedFakeDriver()})
    with pytest.raises(HarnessError, match="UNSET_PROVIDER_KEY_VAR"):
        run(config)


# ------------------------------- builtin-surface calibration probe (Q2 fallback gate)

def test_gating_run_probes_the_driver_before_spending_prompts(tmp_path, fake_drivers):
    config = config_for(tmp_path, manifest_data(), fake_drivers)  # basic is merge_gating
    result = run(config)
    assert result.failures == 0
    probe = json.loads((config.run_dir / "driver-probe" / "fake" / "probe.json").read_text())
    assert probe["verdict"] == "builtins-reported-absent"
    assert probe["positive_control"]["found"] is True
    assert probe["builtins_checked"] == ["FakeWeb"]
    assert probe["builtins_found"] == []
    assert result.driver_probes["fake"]["verdict"] == "builtins-reported-absent"
    run_manifest = json.loads((config.run_dir / "run-manifest.json").read_text())
    assert run_manifest["driver_probes"]["fake"]["path"] == "driver-probe/fake/probe.json"


def test_probe_halts_gating_run_when_a_builtin_is_reported(tmp_path, fake_drivers, monkeypatch):
    monkeypatch.setenv("FAKE_BUILTIN_PRESENT", "1")
    config = config_for(tmp_path, manifest_data(), fake_drivers)
    with pytest.raises(HarnessError, match="FakeWeb"):
        run(config)
    probe = json.loads((config.run_dir / "driver-probe" / "fake" / "probe.json").read_text())
    assert probe["verdict"] == "builtins-present"
    assert probe["builtins_found"] == ["FakeWeb"]
    # No prompt was spent: the probe gate fired before any cell invocation.
    assert not (config.run_dir / "basic").exists()


def test_failed_enumeration_is_broken_never_builtins_absent(tmp_path, fake_drivers, monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_SILENT", "1")
    config = config_for(tmp_path, manifest_data(), fake_drivers)
    with pytest.raises(HarnessError, match="positive control"):
        run(config)
    probe = json.loads((config.run_dir / "driver-probe" / "fake" / "probe.json").read_text())
    assert probe["verdict"] == "broken"


def test_non_gating_run_skips_the_probe(tmp_path, fake_drivers):
    data = manifest_data()
    data["cells"]["basic"]["merge_gating"] = False
    config = config_for(tmp_path, data, fake_drivers)
    result = run(config)
    assert result.driver_probes == {}
    assert not (config.run_dir / "driver-probe").exists()


def test_probe_token_matching_is_whole_word_and_case_sensitive(tmp_path, fake_drivers):
    # The distractor's own tool names contain 'read'/'list'; they must never trip a
    # builtin named 'Read'. Probe a driver whose disallowed list collides in
    # lowercase-substring space only.
    driver = fake_drivers["fake"]

    class CollidingDriver(type(driver)):
        id = "fake"
        disallowed_builtins = ("Read", "List", "Note")

    record = probe_builtin_surface(CollidingDriver(), tmp_path / "probe")
    assert record["verdict"] == "builtins-reported-absent"
    assert record["builtins_found"] == []


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
        "procedure": "neutral-file-triage@2",
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
