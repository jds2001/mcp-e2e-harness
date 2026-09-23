"""Run mechanics: execute the (prompt x cell) grid and record what happened.

The harness executes and records; it never scores (documentation/10-harness.md).
Invariants turned into machine constraints here, each inherited from an incident in
the ancestor harness:

* One fresh process per invocation, never batched -- no two invocations share a trace.
* The consumer runs in an empty working directory with no ``.git``, ``CLAUDE.md``, or
  ``AGENTS.md`` anywhere in its ancestry: the CLI resolves project context by walking
  up, and a repo above the cwd hands the model a developer framing the measurement
  exists to exclude (the ancestor's "I had to identify H.R. 3838 from your git
  history" run).
* Fresh server process and fresh neutral state per invocation; carried-in state
  arrives only via explicit ``setup``/``env`` (ruling S5 -- no warm-by-accident).
* An errored invocation must never look like a consumer that chose to call nothing:
  harness failures are marked as such, and a cell whose every invocation recorded zero
  trace records is reported BROKEN -- never an abstention, a pass, or a vacuous
  result (Layer-1 instrument liveness).
* No configured secret material in any artifact (Layer-1 secret hygiene, not waivable).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, crowding, distractor
from .api_capture import ApiSurfaceRecorder, read_records, summarize, surface_mismatches
from .drivers import DRIVERS, Driver
from .drivers.base import TurnContext
from .manifest import Manifest
from .mcp_client import MCPClientError, StdioMCPClient
from .reporting import finish_reports, write_json
from .secrets import assert_clean, collect_scan_set, is_secret_ref, resolve_env_map

DEFAULT_TIMEOUT_S = 900
CONTEXT_LEAK_MARKERS = (".git", "CLAUDE.md", "AGENTS.md")


class HarnessError(RuntimeError):
    """A configuration or instrument problem that must halt before results exist."""


def _print_log(line: str) -> None:
    print(line, flush=True)


@dataclass(frozen=True)
class Planned:
    entry: dict
    cell_name: str
    cell: dict
    outside_cell_groups: bool


@dataclass
class RunConfig:
    manifest: Manifest
    run_dir: Path
    cells: list[str]
    groups: set[str] | None = None
    prompts: set[str] | None = None
    dry_run: bool = False
    repeats: int | None = None
    precondition_retries: int = 3
    timeout_s: int = DEFAULT_TIMEOUT_S
    drivers: dict[str, Driver] = field(default_factory=lambda: dict(DRIVERS))
    # Run-level spend cap in USD (50-drivers.md loop #6): summed actual usage.cost
    # across the run reaching it stops the run cleanly; completed cells stand and the
    # stop is a run-level outcome. None means no cap. Per-cell caps are the cells'
    # own ``budget_usd``.
    budget_usd: float | None = None
    # Where per-(model, provider, scaffold, driver-version) calibration-probe verdicts
    # are cached (loop #9); default: a ``loop-probe-cache`` directory beside the run
    # directory (its parent), so the cache survives the run and is shared by runs.
    probe_cache_dir: Path | None = None
    # For tests: where neutral cwds are created (must itself be leak-free).
    tmp_base: Path | None = None
    # Operator legibility (ruling S9): the runner is never silent. Every progress
    # line -- run dir up front, cell/prompt starts, turn transitions, per-invocation
    # completion -- goes through this callable as it happens, so in-flight, hung, and
    # broken are distinguishable from the terminal without digging.
    log: Callable[[str], None] = _print_log


@dataclass
class RunResult:
    run_dir: Path
    results: list[dict]
    failures: int
    zero_trace_cells: list[str]
    checks_report: list[dict]
    driver_probes: dict[str, dict] = field(default_factory=dict)
    # Cells BROKEN by the S8 spawn liveness gate, with reasons; their prompts were
    # skipped without spending model turns.
    voided_cells: dict[str, str] = field(default_factory=dict)
    # Budget-cap stops (run-level or per-cell), each a surfaced run-level outcome.
    budget_stops: list[dict] = field(default_factory=list)
    spend_usd: float = 0.0
    # Answerless consumer outcomes (S13): invocation label -> cause. Counted beside
    # pass and fail, never under broken; not harness failures.
    consumer_limits: dict[str, str] = field(default_factory=dict)
    preconditions_unmet: dict[str, dict] = field(default_factory=dict)
    invocation_counts: dict[str, dict] = field(default_factory=dict)
    slots: dict[str, dict] = field(default_factory=dict)


def plan_invocations(manifest: Manifest, cells: list[str],
                     want_groups: set[str] | None,
                     want_prompts: set[str] | None) -> list[Planned]:
    """The (prompt x cell) grid, honoring each cell's group scope and prompt allowlist.

    An id named in ``want_prompts`` runs in every selected cell even when the cell's
    groups exclude it -- an explicit request is a deliberate diagnostic -- and is
    marked ``outside_cell_groups`` so it can never be read back as part of the
    standard grid.
    """
    planned: list[Planned] = []
    for cell_name in cells:
        cell = manifest.cells[cell_name]
        allowed_groups = set(cell["groups"])
        allowlist = set(cell.get("prompts") or [])
        for entry in manifest.prompts:
            explicitly_requested = bool(want_prompts) and entry["id"] in want_prompts
            off_cell = (entry["group"] not in allowed_groups
                        or (bool(allowlist) and entry["id"] not in allowlist))
            if off_cell and not explicitly_requested:
                continue
            if want_groups and entry["group"] not in want_groups:
                continue
            if want_prompts and not explicitly_requested:
                continue
            planned.append(Planned(entry, cell_name, cell, off_cell))
    return planned


def make_neutral_cwd(tag: str, base: Path | None = None) -> Path:
    """An empty working directory with no project context anywhere above it."""
    path = Path(tempfile.mkdtemp(prefix=f"mcpe2e-{tag}-", dir=base))
    resolved = path.resolve()
    for parent in [resolved, *resolved.parents]:
        for leak in CONTEXT_LEAK_MARKERS:
            if (parent / leak).exists():
                raise HarnessError(
                    f"{parent / leak} sits above the neutral working directory {resolved}. "
                    "The consumer's CLI resolves project context by walking up, so it would "
                    "run with that project's context -- the no-disclosure principle forbids "
                    "it. Set TMPDIR to a location with no project above it.")
    return path


def resolve_prompt(entry: dict, cell: dict) -> str:
    """The exact text sent, verbatim. Nothing is appended, ever."""
    variant = cell.get("variant")
    if not variant:
        return entry["prompt"]
    text = (entry.get("variants") or {}).get(variant)
    if not text:
        raise HarnessError(
            f"{entry['id']}: cell requires variant {variant!r} and the prompt does not define it. "
            "Running the base prompt under a variant label would mislabel the measurement.")
    return text


def cell_id_of(cell_name: str, cell: dict, driver: Driver | None = None) -> str:
    surface = cell["tool_surface"]
    surface_tag = "full" if surface == "full" else "surface:" + "+".join(surface)
    parts = [cell["driver"], cell["model"], cell["context"], surface_tag]
    identity = {
        "knobs": cell.get("knobs") or {},
        "setup": cell.get("setup") or [],
        "env": recorded_cell_env(cell),
        "selection": {"groups": sorted(cell.get("groups") or []),
                      "prompts": sorted(cell.get("prompts") or []),
                      "variant": cell.get("variant") or "base"},
    }
    for name, value in identity.items():
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        parts.append(f"{name}:" + hashlib.sha256(canonical.encode()).hexdigest()[:16])
    # Identity components the driver's contract adds (10-harness.md "Cells": the loop
    # driver adds scaffold version and provider pin).
    parts += driver.cell_identity(cell) if driver is not None else []
    return "/".join(parts)


def recorded_cell_env(cell: dict) -> dict:
    """Keep non-secret values and only the destination key of secret entries."""
    return {key: None if is_secret_ref(value) else value
            for key, value in (cell.get("env") or {}).items()}


def row_relative_path(config: RunConfig, cell: str, group: str, prompt: str,
                      repetition: int | None) -> Path:
    path = Path(cell) / group / prompt
    if repetition is not None:
        width = max(2, len(str(config.repeats)))
        path /= f"r{repetition:0{width}d}"
    return path


def meta_relative_path(config: RunConfig, meta: dict) -> Path:
    path = row_relative_path(config, meta["cell"], meta["group"],
                             meta["prompt_id"], meta.get("repetition"))
    return path / f"attempt-{meta['attempt']:02d}" if meta.get("attempt", 1) > 1 else path


def scan_set_for(config: RunConfig, driver: Driver) -> list[str]:
    """The secret-hygiene scan set: the manifest's secrets plus the driver's own
    credentials (``required_env`` values, e.g. the loop's OPENROUTER_API_KEY) -- a
    harness credential reaching an artifact is the same breach as a suite secret."""
    scan = collect_scan_set(config.manifest.data)
    for name in driver.required_env:
        value = os.environ.get(name, "")
        if len(value) >= 8 and value not in scan:
            scan.append(value)
    return scan


def expected_wire_surface(driver: Driver, manifest: Manifest, cell: dict,
                          advertised_tools: list[str] | None,
                          procedure: crowding.CrowdingProcedure | None) -> list[str] | None:
    """The exact tools array a wire_surface_exact driver must send on every request:
    the cell's surface (the list, or every tool the SUT advertised at the spawn
    check for ``full``) plus the distractor's tools in a crowded cell, each in the
    driver's wire naming. None when the surface cannot be stated (no spawn record)."""
    surface = cell["tool_surface"]
    if surface == "full":
        if advertised_tools is None:
            return None
        server_tools = list(advertised_tools)
    else:
        server_tools = list(surface)
    names = [driver.wire_tool_name(manifest.server["name"], t) for t in server_tools]
    if procedure is not None:
        names += [driver.wire_tool_name(procedure.server_name, t) for t in distractor.tool_names(procedure)]
    return sorted(names)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _server_env_spec(manifest: Manifest, cell: dict) -> dict:
    """The env map for the server process: transport env + cell env, placeholders kept.

    ``$secret`` references stay references here -- they are resolved by the proxy (for
    the prompt's server) or by the setup client (in-process), never written resolved.
    """
    transport = manifest.server["transport"]
    env = dict(transport.get("env") or {})
    env.update(cell.get("env") or {})
    return env


def _write_server_config(dest: Path, manifest: Manifest, cell: dict) -> Path:
    transport = manifest.server["transport"]
    config = {
        "command": transport["command"],
        "args": list(transport.get("args") or []),
        "env": _server_env_spec(manifest, cell),
    }
    path = dest / "server-config.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def _proxy_entry(server_config: Path, dest: Path, prefix: str, cell: dict,
                 secrets_file: Path | None = None) -> dict:
    args = ["-m", "mcp_e2e_harness.proxy",
            "--server-config", str(server_config),
            "--trace-file", str(dest / f"{prefix}trace.jsonl"),
            "--tools-file", str(dest / f"{prefix}available-tools.json"),
            "--meta-file", str(dest / f"{prefix}proxy-meta.json")]
    if cell["tool_surface"] != "full":
        args += ["--allow-tools", ",".join(cell["tool_surface"])]
    if secrets_file is not None:
        args += ["--secrets-file", str(secrets_file)]
    return {"command": sys.executable, "args": args}


def _write_secrets_file(env_spec: dict, run_dir: Path) -> Path | None:
    """Transient 0600 KEY=VALUE file carrying the $secret values the proxy needs.

    For drivers that sanitize the environment of MCP servers they spawn (codex,
    50-drivers.md #6): inheritance delivers nothing there, and every channel the
    driver does offer is an artifact, so the values travel by a file OUTSIDE the run
    tree (the run directory is exactly what an operator tars up and attaches to an
    issue), created 0600 by mkstemp, and deleted when the invocation ends. Only the
    file's path appears in the generated config. None when the spec names no secrets.
    """
    names = sorted({value["$secret"] for value in env_spec.values() if is_secret_ref(value)})
    values = {name: os.environ.get(name, "") for name in names}
    values = {k: v for k, v in values.items() if v}
    if not values:
        return None
    fd, raw_path = tempfile.mkstemp(prefix="mcpe2e-secrets-", suffix=".env")
    path = Path(raw_path).resolve()
    if path == run_dir.resolve() or run_dir.resolve() in path.parents:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise HarnessError(
            f"secrets file would land inside the run tree {run_dir}; it must live outside "
            "every artifact directory. Set TMPDIR elsewhere.")
    with os.fdopen(fd, "w") as handle:
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
    return path


def _distractor_entry(procedure: crowding.CrowdingProcedure, dest: Path) -> dict:
    return {"command": sys.executable,
            "args": ["-m", "mcp_e2e_harness.distractor",
                     "--procedure", procedure.full_name,
                     "--state-file", str(dest / "distractor-state.json")]}


def _read_trace(trace_path: Path) -> tuple[list[dict], int]:
    """Parsed trace records and the count of unparseable lines (instrument defects)."""
    records: list[dict] = []
    bad = 0
    if not trace_path.exists():
        return records, bad
    for line in trace_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return records, bad


def _run_setup(manifest: Manifest, cell: dict, dest: Path, scan: list[str]) -> list[dict]:
    """Perform the cell's setup actions by direct MCP calls -- never a model turn.

    A separate server process from the prompt's (S5): effects persist only through
    state the cell's declared env points at. What ran and what it returned is recorded
    in setup.json and scanned for secret material before it is written.
    """
    transport = manifest.server["transport"]
    resolved_env, _ = resolve_env_map(_server_env_spec(manifest, cell), where="setup env")
    env = dict(os.environ)
    env.update(resolved_env)
    records: list[dict] = []
    with StdioMCPClient(transport["command"], list(transport.get("args") or []), env=env) as client:
        for action in cell["setup"]:
            record = client.call_tool(action["tool"], action["args"])
            records.append(record)
            if record["is_error"]:
                _write_scanned(dest / "setup.json", json.dumps(records, indent=2) + "\n", scan)
                raise HarnessError(
                    f"setup action {action['tool']!r} returned an error; the cell's declared "
                    "precondition does not hold. Refusing to run the prompt against a state the "
                    f"cell does not describe. See {dest / 'setup.json'}.")
    _write_scanned(dest / "setup.json", json.dumps(records, indent=2) + "\n", scan)
    return records


def _write_scanned(path: Path, text: str, scan: list[str]) -> None:
    assert_clean(text, scan, str(path))
    path.write_text(text)


class PreconditionUnmet(HarnessError):
    def __init__(self, outcome: dict, record: dict, path: Path):
        self.outcome = {**outcome, "stage": "crowding_preturn"}
        self.record = record
        super().__init__(f"{outcome['cause']}: crowding precondition unmet: "
                         f"{outcome.get('detail')}; scored turn not sent. See {path}.")


def _crowding_preturn(driver: Driver, ctx_base: dict, procedure: crowding.CrowdingProcedure,
                      dest: Path, cwd: Path, timeout_s: int, scan: list[str],
                      session_id: str, crowd_config: Path) -> dict:
    """Run the crowding procedure's opening turn so the scored prompt lands mid-task."""
    ctx = TurnContext(**ctx_base, mcp_config_path=crowd_config,
                      session=("open", session_id), prompt=procedure.opening_prompt)
    turn = driver.build_turn(ctx)
    driver.attribution_record(turn)
    env = dict(os.environ)
    env.update(turn.env_overrides)
    proc = subprocess.run(turn.argv, cwd=cwd, input=turn.stdin_text, env=env,
                          capture_output=True, text=True, timeout=timeout_s)
    record = {
        "procedure": procedure.name,
        "version": procedure.version,
        "content_hash": procedure.content_hash(),
        "opening_prompt": procedure.opening_prompt,
        "exit_status": proc.returncode,
        "answer": proc.stdout,
        "stderr_tail": proc.stderr[-2000:],
    }
    outcome = driver.preturn_outcome(turn, proc.returncode)
    if outcome:
        record["consumer_limit"] = outcome
    state_error = None
    try:
        state_check = procedure.check_state(json.loads((dest / "distractor-state.json").read_text()))
        record["state_check"] = state_check
    except (OSError, ValueError) as exc:
        state_error = f"crowding state unavailable: {dest / 'distractor-state.json'}: {exc}"
        record["state_check"] = {"error": state_error}
    _write_scanned(dest / "crowding.json", json.dumps(record, indent=2) + "\n", scan)
    if state_error:
        raise HarnessError(state_error)
    if proc.returncode != 0 and not outcome:
        raise HarnessError(
            f"crowding pre-turn exited {proc.returncode}; the consumer is NOT mid-task, so "
            "running the scored prompt would mislabel a fresh cell as crowded. See "
            f"{dest / 'crowding.json'}.")
    if outcome or not state_check["passed"]:
        outcome = outcome or {"cause": "state_mismatch", "detail": "filed set differs from the mid-task predicate"}
        raise PreconditionUnmet({**outcome, **state_check}, record, dest / "crowding.json")
    return record


def run_one(config: RunConfig, planned: Planned, driver_versions: dict[str, str],
            advertised_tools: list[str] | None = None,
            repetition: int | None = None, attempt: int = 1) -> dict:
    manifest = config.manifest
    entry, cell_name, cell = planned.entry, planned.cell_name, planned.cell
    driver = config.drivers[cell["driver"]]
    dest = config.run_dir / row_relative_path(
        config, cell_name, entry["group"], entry["id"], repetition)
    if attempt > 1:
        dest /= f"attempt-{attempt:02d}"
    dest.mkdir(parents=True, exist_ok=True)
    scan = scan_set_for(config, driver)

    cwd = make_neutral_cwd(entry["id"], config.tmp_base)
    prompt_text = resolve_prompt(entry, cell)
    server_config = _write_server_config(dest, manifest, cell)
    assert_clean(server_config.read_text(), scan, str(server_config))

    crowded = cell["context"] == "crowded"
    procedure = crowding.get_procedure(cell["crowding"]["procedure"]) if crowded else None

    # Drivers that sanitize spawned-MCP-server environments (codex) get the manifest's
    # $secret values to the proxy via a transient 0600 file outside the run tree --
    # process-env only, never an artifact (50-drivers.md, codex #6).
    secrets_file: Path | None = None
    if driver.sanitizes_mcp_env and not config.dry_run:
        secrets_file = _write_secrets_file(_server_env_spec(manifest, cell), config.run_dir)

    entries = {manifest.server["name"]: _proxy_entry(server_config, dest, "", cell,
                                                     secrets_file=secrets_file)}
    if repetition is not None:
        entries[manifest.server["name"]]["args"].append("--record-server-spawn")
    extra_names: tuple[str, ...] = ()
    if procedure is not None:
        entries[procedure.server_name] = _distractor_entry(procedure, dest)
        extra_names = (procedure.server_name,)
    driver_config = driver.write_driver_config(dest, entries)
    assert_clean(driver_config.read_text(), scan, str(driver_config))

    # API-boundary tool-surface capture (Q2's preferred instrument): the driver's
    # outbound API traffic is routed through a local recording proxy for the whole
    # invocation (pre-turn included), so the tools array actually sent to the model is
    # an observation off the wire, recorded during the scored turn with zero
    # disclosure -- never a model claim. Started before the turn is built: drivers
    # route to it in their own idiom (claude via env var, codex via the custom
    # provider's base_url), so the URL is part of the turn's construction.
    recorder = None
    api_base_url: str | None = None
    if driver.supports_api_capture and not config.dry_run:
        upstream = ((os.environ.get(driver.api_base_env) if driver.api_base_env else None)
                    or driver.api_default_upstream)
        recorder = ApiSurfaceRecorder(dest / "api-surface.jsonl", upstream)
        api_base_url = recorder.start()

    ctx_base = dict(model=cell["model"], knobs=cell["knobs"],
                    server_name=manifest.server["name"], tool_surface=cell["tool_surface"],
                    extra_server_names=extra_names, dest=dest, api_base_url=api_base_url,
                    cell=cell)

    started = _utcnow()
    t0 = time.perf_counter()
    harness_failure: str | None = None
    consumer_limit: dict | None = None
    upstream_unavailable = None
    upstream_failure = None
    precondition_unmet = None
    precondition_failure = None
    scored_turn_reached = False
    exit_status = -1
    answer = ""
    stderr_text = ""
    stdout_text = ""
    web_activity: list[str] = []
    setup_records: list[dict] = []
    crowding_record: dict | None = None
    session = ("single", "")

    # Build the scored turn up front so argv-level assertions run even on --dry-run.
    if crowded:
        session_id = str(uuid.uuid4())
        crowd_entries = dict(entries)
        crowd_entries[manifest.server["name"]] = _proxy_entry(server_config, dest, "crowding-", cell,
                                                              secrets_file=secrets_file)
        crowd_config = driver.write_driver_config(dest, crowd_entries)
        # Both turns of a crowded invocation share one config file name per driver, so
        # give the pre-turn config its own path.
        crowd_config = crowd_config.rename(dest / "mcp-config-crowding.json")
        driver_config = driver.write_driver_config(dest, entries)
        session = ("resume", session_id)
    scored_ctx = TurnContext(**ctx_base, mcp_config_path=driver_config,
                             session=session, prompt=prompt_text)
    turn = driver.build_turn(scored_ctx)
    attribution = driver.attribution_record(turn)
    assert_clean("\n".join(turn.argv), scan, "runner argv")
    assert_clean(json.dumps(turn.env_overrides), scan, "turn env overrides")
    turn_env = dict(os.environ)
    turn_env.update(turn.env_overrides)

    if config.dry_run:
        exit_status, answer = 0, "[dry-run: no model was called]"
    else:
        try:
            if cell.get("setup"):
                config.log(f"     setup: {len(cell['setup'])} direct server-side action(s)")
                setup_records = _run_setup(manifest, cell, dest, scan)
            if crowded and procedure is not None:
                config.log(f"     crowding pre-turn: {procedure.full_name}")
                crowding_record = _crowding_preturn(
                    driver, ctx_base, procedure, dest, cwd, config.timeout_s, scan,
                    session_id, dest / "mcp-config-crowding.json")
            scored_turn_reached = True
            config.log(f"     scored turn: {cell['driver']} {cell['model']}")
            proc = subprocess.run(turn.argv, cwd=cwd, input=turn.stdin_text, env=turn_env,
                                  capture_output=True, text=True, timeout=config.timeout_s)
            exit_status = proc.returncode
            stderr_text = proc.stderr
            stdout_text = proc.stdout
            if turn.answer_from == "stdout":
                answer = proc.stdout
            elif turn.answer_path and turn.answer_path.exists():
                answer = turn.answer_path.read_text()
            elif turn.answer_from != "stdout":
                harness_failure = "answer file missing -- the consumer output could not be read"
            if exit_status != 0:
                harness_failure = f"runner exited {exit_status}: {stderr_text[-800:]}"
            elif not answer and harness_failure is None:
                consumer_limit = {
                    "cause": "null_final_content",
                    "detail": "consumer completed with an empty answer",
                    "reasoning_tail_json": None,
                    "reasoning_tail_matches_tools": None,
                }
        except PreconditionUnmet as exc:
            precondition_unmet = exc.outcome
            crowding_record = exc.record
            harness_failure = precondition_failure = str(exc)
        except subprocess.TimeoutExpired as exc:
            harness_failure = f"timeout after {config.timeout_s}s -- harness failure, NOT a consumer result"
            partial = exc.stderr
            stderr_text = (partial.decode(errors="replace")
                           if isinstance(partial, bytes) else (partial or ""))
        except FileNotFoundError as exc:
            harness_failure = f"runner not found: {exc}"
        except (HarnessError, MCPClientError) as exc:
            harness_failure = str(exc)
        finally:
            if recorder is not None:
                recorder.stop()
            if secrets_file is not None:
                secrets_file.unlink(missing_ok=True)
        for transcript_path in driver.transcript_paths(dest):
            assert_clean(transcript_path.read_text(errors="replace"), scan, str(transcript_path))
        _write_scanned(dest / "runner-stderr.txt", stderr_text, scan)
        _write_scanned(dest / "answer.txt", answer, scan)
        if turn.answer_from != "stdout":
            # The driver's stdout is its event stream (not the answer): keep it -- it
            # is where codex reports MCP startup failures and web-tool events.
            _write_scanned(dest / "runner-stdout.txt", stdout_text, scan)

    # The driver family's own reading of the invocation's artifacts (the loop
    # driver: served providers per request against the pin, usage and cost sums,
    # loop mechanics, the consumer's fail-fast breach). Any breach voids the cell.
    family_record: dict | None = None
    breaches: list[str] = []
    spend_usd: float | None = None
    if not config.dry_run:
        extra = driver.after_turn(turn, dest, cell, dest / "api-surface.jsonl", cell_name)
        if extra is not None:
            family_record = extra.get("record")
            breaches = list(extra.get("breaches") or [])
            spend_usd = extra.get("cost_usd")
            consumer_limit = extra.get("consumer_limit") or consumer_limit
            upstream_unavailable = extra.get("upstream_unavailable")
            if upstream_unavailable and harness_failure and harness_failure.startswith(
                    ("runner exited", "crowding pre-turn exited")):
                status = upstream_unavailable["http_status"] or "timeout"
                provider = upstream_unavailable["provider"] or "unknown"
                harness_failure = upstream_failure = (
                    f"upstream_unavailable: provider={provider} HTTP {status} after "
                    f"{upstream_unavailable['attempts']} attempts")
            if breaches:
                harness_failure = breaches[0]
            # S13/S16 consumer outcomes use a nonzero exit status deliberately.
            # Lift that provisional failure before checking the instruments; a
            # breach or any unrelated failure remains an instrument failure.
            if (consumer_limit and not breaches and harness_failure
                    and harness_failure.startswith("runner exited")):
                harness_failure = None

    # Effect-level web-activity scan over the driver's EVENT streams (never answer
    # content -- an answer saying "I cannot search the web" must not read as web
    # activity): stderr always, stdout only when the answer travels by file. Any hit
    # is an instrument breach (50-drivers.md, codex #5).
    if driver.web_event_markers and not config.dry_run:
        streams = [stderr_text] + ([stdout_text] if turn.answer_from != "stdout" else [])
        lowered = [s.casefold() for s in streams]
        web_activity = [m for m in driver.web_event_markers
                        if any(m in s for s in lowered)]
        if web_activity and (harness_failure is None or precondition_unmet or upstream_failure):
            harness_failure = (f"web-tool activity suspected in the driver's event streams "
                               f"({','.join(web_activity)}) -- instrument breach; claims not "
                               "tool-attributable")

    api_surface: dict | None = None
    if recorder is not None:
        api_surface = summarize(dest / "api-surface.jsonl", driver.disallowed_builtins) or {
            "requests_recorded": 0, "requests_readable": 0, "requests_with_tools": 0,
            "tool_names_union": [], "disallowed_builtins_on_wire": [],
            "count_tokens_calls": 0, "count_tokens_unread": 0}
        api_surface["unparseable_requests"] = recorder.unrecorded_requests
        api_surface["unparseable_encodings"] = sorted(recorder.unrecorded_encodings)
        api_surface["upstream_forward_errors"] = recorder.forward_errors
        if harness_failure is None or precondition_unmet or upstream_failure:
            if api_surface["disallowed_builtins_on_wire"]:
                # The channel the argv claims closed is open at the wire: an instrument
                # breach, not a consumer behavior; the row's claims are not
                # attribution-scoreable (the ancestor's F30 shape).
                harness_failure = (
                    "disallowed builtin(s) "
                    f"{api_surface['disallowed_builtins_on_wire']} observed in the tools array "
                    "sent to the model -- instrument breach; claims not tool-attributable")
            elif driver.wire_surface_exact:
                # S7 in its exact form (50-drivers.md loop #3): the tools array on
                # EVERY request equals the cell's surface; any extra name is
                # endpoint- or router-injected, any missing name is a surface the
                # consumer did not offer. Either way the cell is BROKEN.
                expected = expected_wire_surface(driver, manifest, cell, advertised_tools, procedure)
                api_surface["expected_wire_surface"] = expected
                if expected is None:
                    api_surface["wire_surface_mismatches"] = None
                    harness_failure = ("the expected wire surface could not be stated (no spawn "
                                       "record for a full-surface cell); S7 unverified")
                else:
                    records = read_records(dest / "api-surface.jsonl")
                    mismatches = surface_mismatches(records, expected)
                    api_surface["wire_surface_mismatches"] = mismatches
                    if not records:
                        harness_failure = ("API surface capture recorded zero readable model "
                                           "requests: the surface is unverified for this invocation")
                    elif mismatches:
                        harness_failure = (
                            f"wire tools array differs from the cell's surface on {len(mismatches)} "
                            f"request(s) (first: seq {mismatches[0]['seq']}, extra "
                            f"{mismatches[0]['extra']}, missing {mismatches[0]['missing']}) -- "
                            "instrument breach; claims not tool-attributable")
            elif api_surface["requests_readable"] == 0:
                # An answer with zero readable model requests means the driver either
                # did not route through the capture or sent nothing the recorder could
                # read; unverified must never read as verified. A tool-less request is
                # recorded (tool_names null) and does NOT trip this: a request offering
                # no tools offers no builtins. An unparseable body IS recorded too (at
                # wire size, for context accounting) but is not readable: no surface
                # could be read off it, so it counts for nothing here.
                detail = (f" ({api_surface['unparseable_requests']} request(s) arrived but "
                          f"could not be parsed; encodings seen: "
                          f"{api_surface['unparseable_encodings']})"
                          if api_surface["unparseable_requests"] else "")
                harness_failure = (
                    "API surface capture recorded zero readable model requests: builtin "
                    f"absence is unverified for this invocation{detail}")

    # Read AFTER the turn executes: vendor-pushed state (e.g. codex's own plugin-sync
    # fetch) lands mid-invocation, never at build_turn time (50-drivers.md Residual 2).
    environment_state = driver.environment_state(turn) if not config.dry_run else None

    if api_surface and api_surface.get("wire_surface_mismatches"):
        breaches.append("wire tools array differs from the cell's surface (S7 exact)")
    if api_surface and api_surface.get("disallowed_builtins_on_wire"):
        breaches.append(f"disallowed builtin(s) on the wire: {api_surface['disallowed_builtins_on_wire']}")
    marks = driver.cell_marks(cell)

    duration = round(time.perf_counter() - t0, 2)
    records, parse_errors = _read_trace(dest / "trace.jsonl")
    if (dest / "trace.jsonl").exists():
        assert_clean((dest / "trace.jsonl").read_text(errors="replace"), scan, str(dest / "trace.jsonl"))
    if parse_errors and (not harness_failure or precondition_unmet or upstream_failure):
        harness_failure = (f"{parse_errors} unparseable trace line(s) -- instrument defect; "
                           "fix the instrument before any disposition")

    if upstream_failure and api_surface and api_surface.get("upstream_forward_errors"):
        harness_failure = "API recorder forwarding error; upstream availability is not attributable"
    if upstream_failure and harness_failure != upstream_failure:
        upstream_unavailable = None  # Instrument defects must never enable replacement.
    if precondition_unmet and harness_failure != precondition_failure:
        precondition_unmet = None  # An instrument defect takes precedence.

    tools_path = dest / "available-tools.json"
    recorded_surface = json.loads(tools_path.read_text()) if tools_path.exists() else None

    zero_trace_liveness = None
    if not config.dry_run and not records and (consumer_limit or {}).get("cause") == "null_final_content":
        zero_trace_liveness = null_final_liveness(
            config, cell_name, dest, driver.family, api_surface, harness_failure, breaches)
        if not zero_trace_liveness["exempted"] and harness_failure is None:
            harness_failure = (
                "zero-trace null final is BROKEN: exemption requires a loop row, a passed spawn check, "
                "an exact wire surface on every request, and a completed 2xx final response with finish_reason")

    # Row measurements (30-checks.md, S14): mechanical values across a trace record
    # and this row's answer, recorded beside the row with the method's name and hash.
    # Never an outcome; nothing here reaches checks-report.json.
    row_measurements = {}  # Derived only after the run manifest checkpoint exists.

    meta = {
        "prompt_id": entry["id"],
        "group": entry["group"],
        "cell": cell_name,
        "cell_id": cell_id_of(cell_name, cell, driver),
        "repetition": repetition,
        "env": recorded_cell_env(cell),
        "driver": {"id": cell["driver"], "cli_version": driver_versions.get(cell["driver"])},
        # S11 marks: the family on every row ("product" | "loop"); loop rows add the
        # scaffold with hash, the pin (slug verified, quantization asserted) or
        # unpinned, and the reproducibility mark.
        "driver_family": marks["driver_family"],
        **({"scaffold": marks["scaffold"], "provider_pin": marks["provider_pin"],
            "quantization_asserted": marks["quantization_asserted"],
            "reproducibility": marks["reproducibility"]}
           if "scaffold" in marks else {}),
        "model": cell["model"],
        # The cell's knobs in the driver's native vocabulary, verbatim -- never
        # translated, never defaulted by the harness.
        "knobs": cell["knobs"],
        "role": cell["role"],
        "context": cell["context"],
        "tool_surface": cell["tool_surface"],
        "variant": cell.get("variant") or "base",
        "outside_cell_groups": planned.outside_cell_groups,
        "manifest_sha256": manifest.sha256,
        "fixture": entry.get("fixture"),
        "prompt_sent": prompt_text,
        "started_utc": started,
        "finished_utc": _utcnow(),
        "duration_s": duration,
        "exit_status": exit_status,
        "harness_failure": harness_failure,
        "precondition_unmet": precondition_unmet,
        "upstream_unavailable": upstream_unavailable,
        "attempt": attempt,
        "scored_turn_reached": scored_turn_reached,
        # S13/S16: answerless consumer outcomes, including limits and null final content.
        # answer.txt is empty for such a row; this is where it says so.
        "consumer_limit": consumer_limit,
        **({"zero_trace_liveness": zero_trace_liveness} if zero_trace_liveness is not None else {}),
        "trace_records": len(records),
        "trace_parse_errors": parse_errors,
        "tool_calls": [r.get("tool", "?") for r in records],
        "answer_chars": len(answer),
        # A digest of answer.txt (sha256, first 16 hex): the per-cell distinct-answer
        # count in run-manifest.json is computed from these (WO-3).
        "answer_sha256_16": (hashlib.sha256(answer.encode("utf-8", "replace")).hexdigest()[:16]
                             if answer else None),
        # measurements.<name>: one entry per record the declaration's selector matched,
        # each carrying the method name and content hash beside the values (null
        # values with a note when the reference pointer does not resolve to a string).
        "measurements": row_measurements,
        "command": turn.argv,
        "cwd": str(cwd),
        "recorded_tool_surface": recorded_surface,
        # Wire-level observation of the tools array sent to the model (Q2's preferred
        # instrument); null when the driver does not support capture or on a dry run.
        "api_surface": api_surface,
        # Web-tool markers found in the driver's own event streams; non-empty means
        # the row's claims are not tool-attributable (instrument breach).
        "web_activity_suspected": web_activity,
        "attribution": attribution,
        # Executed environment material the runner applied for this turn (e.g. the
        # isolated CODEX_HOME, the recorder URL). Never carries secret values.
        "env_overrides": turn.env_overrides,
        # Vendor-pushed state observed in the invocation's environment after the turn
        # ran, outside anything the harness configured (e.g. codex's own plugin-sync
        # fetch into CODEX_HOME); null for drivers with nothing of this kind to report.
        "environment_state": environment_state,
        # Instrument breaches found on this invocation (each voids the cell).
        "breaches": breaches,
        # Actual spend on this invocation from the recorder's usage.cost lines
        # (loop cells); null for drivers that report none.
        "spend_usd": spend_usd,
        # The driver family's own record (the loop driver's served providers, usage,
        # steps, probe, marks); absent for product drivers.
        **({driver.family: family_record} if family_record is not None else {}),
        "setup": setup_records,
        "crowding": ({"procedure": procedure.name, "version": procedure.version,
                      "content_hash": procedure.content_hash(),
                      "collision_review": cell["crowding"]["collision_review"],
                      "state_check": crowding_record.get("state_check") if crowding_record else None,
                      "preturn_ok": bool(crowding_record and crowding_record["exit_status"] == 0
                                         and crowding_record["state_check"]["passed"])}
                     if procedure is not None else None),
        # Criteria travel WITH the result so a scorer never has to reconstruct them,
        # and so editing one after seeing a result is visible in the diff.
        "criteria": {k: entry.get(k) for k in
                     ("title", "pass", "fail", "rubric", "watch", "grounding", "sourcing")},
    }
    _write_scanned(dest / "meta.json", json.dumps(meta, indent=2) + "\n", scan)
    return meta


def answers_by_prompt(results: list[dict], cell_name: str) -> dict[str, dict]:
    """The distinct-answer count per prompt id within one cell (loop requirement 11, WO-3/WO-4).

    Requirement 11 concerns repeats of *one* prompt, so the count is keyed by prompt id:
    ``invocations`` counts attempts that reached the scored turn; ``answered`` counts non-empty answers. The
    digest map shows which answered rows repeat without opening answer files. The
    across-prompts total lives beside it under its own clearly labeled key, never here.
    """
    answers: dict[str, dict] = {}
    for meta in results:
        if meta["cell"] != cell_name:
            continue
        summary = answers.setdefault(meta["prompt_id"], {
            "invocations": 0, "answered": 0, "distinct": 0, "digests": {}})
        if not meta.get("scored_turn_reached", True):
            continue
        summary["invocations"] += 1
        if not meta.get("answer_chars"):
            continue
        summary["answered"] += 1
        digest = meta["answer_sha256_16"]
        summary["digests"][digest] = summary["digests"].get(digest, 0) + 1
        summary["distinct"] = len(summary["digests"])
    return answers


def answers_across_prompts(by_prompt: dict[str, dict]) -> dict:
    """The cell-level total over every prompt: labeled as such because two prompts whose
    answers matched is not a repeat of one prompt."""
    union = {digest for entry in by_prompt.values() for digest in entry["digests"]}
    return {"invocations": sum(entry["invocations"] for entry in by_prompt.values()),
            "distinct": len(union)}


def null_final_liveness(config: RunConfig, cell_name: str, dest: Path,
                        driver_family: str, api_surface: dict | None,
                        harness_failure: str | None, breaches: list[str]) -> dict:
    """WO-8: record positive, row-local evidence before exempting a null final."""
    spawn_path = config.run_dir / cell_name / "spawn-check.json"
    try:
        spawn = json.loads(spawn_path.read_text())
    except (OSError, ValueError):
        spawn = {}
    spawn_passed = isinstance(spawn, dict) and spawn.get("ok") is True
    wire, bad_lines = _read_trace(dest / "api-surface.jsonl")
    readable = all(isinstance(line, dict) for line in wire)
    wire = [line for line in wire if isinstance(line, dict)]
    expected = (api_surface or {}).get("expected_wire_surface")
    surface_exact = (isinstance(expected, list) and bool(wire) and not bad_lines and readable
                     and all(isinstance(line.get("tool_names"), list) for line in wire)
                     and not surface_mismatches(wire, expected))
    final = wire[-1] if wire else {}
    status, finish_reason = final.get("http_status"), final.get("finish_reason")
    final_observed = (not bad_lines and readable and type(status) is int and 200 <= status < 300
                      and isinstance(finish_reason, str) and bool(finish_reason)
                      and final.get("finish_reason_note") is None
                      and final.get("duration_ms") is not None and final.get("duration_note") is None)
    return {
        "exempted": (driver_family == "loop" and spawn_passed and surface_exact and final_observed
                     and harness_failure is None and not breaches),
        "spawn_check_passed": spawn_passed,
        "spawn_check_path": str(spawn_path.relative_to(config.run_dir)),
        "wire_surface_exact": surface_exact,
        "wire_requests": len(wire),
        "expected_wire_surface": expected,
        "final_response_observed": final_observed,
        "final_response": {"seq": final.get("seq"), "http_status": status, "finish_reason": finish_reason},
    }


def zero_trace_cells(results: list[dict], dry_run: bool) -> list[str]:
    """Cells where every invocation recorded zero trace records: BROKEN instruments.

    An empty trace normally fails Layer-1 liveness. S16 exempts an observed null
    final message with no instrument failure: that is an answerless consumer outcome.
    """
    if dry_run:
        return []
    totals: dict[str, int] = {}
    for meta in results:
        totals[meta["cell"]] = totals.get(meta["cell"], 0) + meta["trace_records"]
    # Positive evidence establishes cell liveness, but cannot lift any sibling's
    # row-level harness failure: run_one evaluates each null final independently.
    exempt_cells = {m["cell"] for m in results
                    if m.get("driver_family") == "loop"
                    and (m.get("consumer_limit") or {}).get("cause") == "null_final_content"
                    and (m.get("zero_trace_liveness") or {}).get("exempted") is True
                    and not m.get("harness_failure") and not m.get("breaches")}
    return sorted(cell for cell, total in totals.items()
                  if total == 0 and cell not in exempt_cells)


def write_cell_void(run_dir: Path, cell_name: str, reason: str) -> None:
    """The cell-level BROKEN marker, written INTO the artifacts where no per-row
    reading can miss it -- a verdict living only in an exit code gets scored clean."""
    dest = run_dir / cell_name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "CELL-VOID.json").write_text(json.dumps({
        "cell": cell_name,
        "verdict": "BROKEN",
        "reason": reason,
        "scoring_rule": "Do not score any row of this cell as a consumer result.",
    }, indent=2) + "\n")


# ------------------------------------------- spawn liveness (ruling S8, from DR-1)

def spawn_liveness_check(manifest: Manifest, cell: dict, dest_dir: Path,
                         scan: list[str], tmp_base: Path | None = None) -> dict:
    """Fail-fast instrument liveness at spawn: is the SUT alive under cell conditions?

    Grounded in DR-1 (documentation/90-open-questions.md): the first real suite's
    server command was cwd-dependent, died with exit 2 at every spawn, and the harness
    still spent full model turns whose answers came from priors -- three cells
    completed wearing harness_failure: null. Per S8, a SUT that exits or fails to
    advertise tools before the consumer turn begins BREAKS the cell immediately,
    without spending the model turn.

    Runs from a fresh NEUTRAL cwd with the cell's resolved env -- the invocation's own
    conditions; a check from the harness's cwd would have passed while every real turn
    failed, which is exactly the DR-1 mechanism. The check spawns its own short-lived
    server process (permitted like setup: harness-side, direct, recorded) and asks for
    initialize + tools/list; breach conditions are spawn/initialize failure, zero
    advertised tools, and -- for list-valued surfaces -- a surface tool the server
    does not advertise (the effective surface would be empty of it: the same
    dead-instrument shape).
    """
    transport = manifest.server["transport"]
    resolved_env, _ = resolve_env_map(_server_env_spec(manifest, cell), where="spawn-check env")
    env = dict(os.environ)
    env.update(resolved_env)
    cwd = make_neutral_cwd("spawncheck", tmp_base)
    record: dict = {"at": _utcnow(), "cwd": str(cwd)}
    client = StdioMCPClient(transport["command"], list(transport.get("args") or []),
                            env=env, cwd=str(cwd), timeout=60)
    try:
        client.start()
        names = [t.get("name") for t in client.list_tools()]
        record["advertised_tools"] = names
        surface = cell["tool_surface"]
        missing = [t for t in surface if t not in names] if surface != "full" else []
        if not names:
            record.update(ok=False, reason="the server advertises zero tools; every prompt "
                                           "would run against an empty surface")
        elif missing:
            record.update(ok=False,
                          reason=f"tool_surface names {missing} that the server does not "
                                 "advertise; the cell's effective surface would be missing them")
        else:
            record.update(ok=True, reason=None)
    except MCPClientError as exc:
        record.update(ok=False, advertised_tools=None,
                      reason=f"the server failed at spawn: {exc}")
    finally:
        client.close()
        record["stderr_tail"] = "".join(getattr(client, "stderr_tail", []) or [])[-1200:]
    dest_dir.mkdir(parents=True, exist_ok=True)
    _write_scanned(dest_dir / "spawn-check.json", json.dumps(record, indent=2) + "\n", scan)
    return record


# -------------------------- builtin-surface calibration probe (Q2 fallback instrument)

# Instrument ranking (Q2 ruling, 455568a): the API-boundary capture is preferred (wire
# ground truth, zero disclosure, runs on scored cells); THIS probe is the fallback for
# drivers whose API traffic the harness cannot interpose. It is a CALIBRATION run -- a
# separate, discarded invocation under the identical configuration; the scored
# consumer never sees it -- so it breaches no-disclosure only if misused inside a
# scored turn, which the harness never does. Its two stated caveats: it relies on the
# driver constructing the surface deterministically from configuration (so it is valid
# per recorded cli_version and must be re-run when that changes), and the enumeration
# is model-mediated (it can omit, paraphrase, or hallucinate). The probe measures the
# INSTRUMENT, never a consumer: like the ancestor's canary it may name what it needs.
PROBE_PROMPT = (
    "This is an instrument check, not a task. Reply with the exact names of every tool "
    "available to you in this session, one name per line and nothing else. Include every "
    "MCP tool and every built-in tool you are able to call."
)
PROBE_SERVER_NAME = "probe_target"
# A tool name the enumeration must contain for the probe to count as an enumeration at
# all: the harness's own distractor server is registered as the MCP target, so its
# tools are known-present. Without this positive control, a refusal, an error, or an
# empty answer would read as "no builtins" -- a scan that errors must never look like
# one that found nothing.
PROBE_POSITIVE_CONTROL = "list_unfiled_notes"

PROBE_PASS = "builtins-reported-absent"

_PROBE_CAVEAT = (
    "Calibration evidence, model-mediated: the verdict is over the consumer's own "
    "enumeration of its tool surface in a discarded invocation under the identical "
    "configuration -- absence of mention is not proof of absence, and validity rests "
    "on the driver building the surface deterministically from configuration, so this "
    "measurement is per cli_version and must be re-run when that changes. It pairs "
    "with the argv-asserted attribution record. Where the driver supports it, the "
    "API-boundary capture (api-surface.jsonl per invocation) supersedes this probe "
    "with wire-level ground truth (Q2 ruling, documentation/90-open-questions.md). "
    f"A verdict other than '{PROBE_PASS}' refuses merge-gating cells as "
    "instrument-broken."
)


def probe_builtin_surface(driver: Driver, dest: Path, *, model: str | None = None,
                          timeout_s: int = DEFAULT_TIMEOUT_S, tmp_base: Path | None = None,
                          scan: list[str] | None = None) -> dict:
    """Measure whether the driver's disallowed builtins reach the consumer's surface.

    Runs one turn under the exact isolation argv the cells use, against the harness's
    own distractor server, asking the consumer to enumerate its tools. Verdicts:

    * ``builtins-reported-absent`` -- enumeration happened (positive control present)
      and no disallowed builtin name appears in it.
    * ``builtins-present`` -- a disallowed builtin appears: the channel the argv
      claims to close is open. Instrument defect.
    * ``broken`` -- the probe could not measure (runner failure, or no enumeration);
      never read as builtins-absent.
    """
    model = model or driver.probe_model
    if not model:
        raise HarnessError(
            f"driver {driver.id!r} defines no probe model; pass one explicitly. An unprobeable "
            "driver may not host merge-gating cells.")
    if not driver.disallowed_builtins:
        raise HarnessError(
            f"driver {driver.id!r} declares no disallowed builtins to probe for -- the probe "
            "would be a zero-denominator claim.")
    scan = scan or []
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    cwd = make_neutral_cwd("probe", tmp_base)
    server_config = dest / "server-config.json"
    server_config.write_text(json.dumps({
        "command": sys.executable,
        "args": ["-m", "mcp_e2e_harness.distractor",
                 "--procedure", crowding.NEUTRAL_FILE_TRIAGE_V2.full_name],
        "env": {},
    }, indent=2) + "\n")
    entries = {PROBE_SERVER_NAME: _proxy_entry(server_config, dest, "", {"tool_surface": "full"})}
    config_path = driver.write_driver_config(dest, entries)

    ctx = TurnContext(model=model, knobs={}, mcp_config_path=config_path,
                      server_name=PROBE_SERVER_NAME, tool_surface="full",
                      prompt=PROBE_PROMPT, dest=dest)
    turn = driver.build_turn(ctx)
    attribution = driver.attribution_record(turn)
    probe_env = dict(os.environ)
    probe_env.update(turn.env_overrides)

    started = _utcnow()
    answer = ""
    stderr_text = ""
    exit_status = -1
    detail: str | None = None
    try:
        proc = subprocess.run(turn.argv, cwd=cwd, input=turn.stdin_text, env=probe_env,
                              capture_output=True, text=True, timeout=timeout_s)
        exit_status = proc.returncode
        stderr_text = proc.stderr
        if turn.answer_from == "stdout":
            answer = proc.stdout
        elif turn.answer_path and turn.answer_path.exists():
            answer = turn.answer_path.read_text()
    except subprocess.TimeoutExpired:
        detail = f"probe timed out after {timeout_s}s"
    except FileNotFoundError as exc:
        detail = f"driver executable not found: {exc}"
    _write_scanned(dest / "answer.txt", answer, scan)
    _write_scanned(dest / "runner-stderr.txt", stderr_text, scan)

    found: list[str] = []
    if detail is not None:
        verdict = "broken"
    elif exit_status != 0:
        verdict, detail = "broken", f"probe runner exited {exit_status}: {stderr_text[-400:]}"
    elif PROBE_POSITIVE_CONTROL not in answer:
        verdict = "broken"
        detail = (f"positive control {PROBE_POSITIVE_CONTROL!r} absent from the answer: the "
                  "enumeration did not happen (refusal, empty answer, or MCP surface missing). "
                  "An un-run enumeration must never read as builtins-absent.")
    else:
        # Case-sensitive whole-token match, so 'read_note' never trips 'Read'. A false
        # positive fails loudly and gets investigated -- the safe direction.
        found = [name for name in driver.disallowed_builtins
                 if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", answer)]
        verdict = "builtins-present" if found else PROBE_PASS
        if found:
            detail = f"disallowed builtin(s) {found} appear in the consumer's own tool enumeration"

    record = {
        "driver": {"id": driver.id, "cli_version": driver.cli_version()},
        "model": model,
        "prompt": PROBE_PROMPT,
        "command": turn.argv,
        "attribution": attribution,
        "environment_state": driver.environment_state(turn),
        "started_utc": started,
        "finished_utc": _utcnow(),
        "exit_status": exit_status,
        "answer_chars": len(answer),
        "positive_control": {"expected": PROBE_POSITIVE_CONTROL,
                             "found": PROBE_POSITIVE_CONTROL in answer},
        "builtins_checked": list(driver.disallowed_builtins),
        "builtins_found": found,
        "verdict": verdict,
        "detail": detail,
        "caveat": _PROBE_CAVEAT,
    }
    _write_scanned(dest / "probe.json", json.dumps(record, indent=2) + "\n", scan)
    return record


# --------------------------------------------- egress canary (codex Q2 residual)

# Instrument check, never a consumer measurement: it names exactly what it needs.
# The exact-string failure protocol is the handoff's probe recipe; ONLY that honest
# failure passes. A reported fetch result fails the canary whether the egress was
# real or fabricated -- both disqualify attribution, and telling them apart is what
# the recorded event stream and wire capture beside the verdict are for.
EGRESS_PROBE_URL = "https://example.com/"
EGRESS_PROBE_PROMPT_TEMPLATE = (
    "This is an instrument check, not a task. Using your shell tool, run exactly: "
    "curl -sS -m 10 -i {url} and report the HTTP status line you received, verbatim. "
    "If the command cannot reach the network at all, reply with exactly: "
    "NO NETWORK ACCESS"
)
EGRESS_PASS = "egress-blocked-reported"


def probe_egress(driver: Driver, dest: Path, *, model: str | None = None,
                 url: str = EGRESS_PROBE_URL, timeout_s: int = DEFAULT_TIMEOUT_S,
                 tmp_base: Path | None = None, scan: list[str] | None = None) -> dict:
    """Measure, under harness instruments, that shell-shaped egress fails honestly.

    50-drivers.md (codex, Q2-settling residual): the code-mode surface retains
    exec_command/apply_patch -- network-capable channels whose egress control is the
    driver's sandbox, previously measured blocked only externally. One turn under the
    exact cell configuration asks the consumer to fetch a pinned URL via its shell.
    Verdicts:

    * ``egress-blocked-reported`` -- the consumer reported the exact honest-failure
      protocol string. The sandbox blocked egress, observed end to end.
    * ``egress-not-verified-blocked`` -- anything else came back: a fetched result,
      a fabricated one, or a refusal to try. All disqualifying; the artifacts beside
      the verdict (event stream, wire capture, answer verbatim) carry the diagnosis.
    * ``broken`` -- the probe could not measure (runner failure, empty answer,
      disallowed tool on the wire); never read as blocked.
    """
    model = model or driver.egress_probe_model
    if not model:
        raise HarnessError(
            f"driver {driver.id!r} defines no egress-probe model. Either no shell-shaped "
            "channel survives its surface removal (the canary does not apply) or the driver "
            "cannot be probed; pass --model to override.")
    scan = scan or []
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    cwd = make_neutral_cwd("egress", tmp_base)
    server_config = dest / "server-config.json"
    server_config.write_text(json.dumps({
        "command": sys.executable,
        "args": ["-m", "mcp_e2e_harness.distractor",
                 "--procedure", crowding.NEUTRAL_FILE_TRIAGE_V2.full_name],
        "env": {},
    }, indent=2) + "\n")
    entries = {PROBE_SERVER_NAME: _proxy_entry(server_config, dest, "", {"tool_surface": "full"})}
    config_path = driver.write_driver_config(dest, entries)

    recorder = None
    api_base_url = None
    if driver.supports_api_capture:
        upstream = ((os.environ.get(driver.api_base_env) if driver.api_base_env else None)
                    or driver.api_default_upstream)
        recorder = ApiSurfaceRecorder(dest / "api-surface.jsonl", upstream)
        api_base_url = recorder.start()

    prompt = EGRESS_PROBE_PROMPT_TEMPLATE.format(url=url)
    ctx = TurnContext(model=model, knobs={}, mcp_config_path=config_path,
                      server_name=PROBE_SERVER_NAME, tool_surface="full",
                      prompt=prompt, dest=dest, api_base_url=api_base_url)
    turn = driver.build_turn(ctx)
    attribution = driver.attribution_record(turn)
    probe_env = dict(os.environ)
    probe_env.update(turn.env_overrides)

    started = _utcnow()
    answer = ""
    stdout_text = ""
    stderr_text = ""
    exit_status = -1
    detail: str | None = None
    try:
        proc = subprocess.run(turn.argv, cwd=cwd, input=turn.stdin_text, env=probe_env,
                              capture_output=True, text=True, timeout=timeout_s)
        exit_status = proc.returncode
        stdout_text, stderr_text = proc.stdout, proc.stderr
        if turn.answer_from == "stdout":
            answer = proc.stdout
        elif turn.answer_path and turn.answer_path.exists():
            answer = turn.answer_path.read_text()
    except subprocess.TimeoutExpired:
        detail = f"probe timed out after {timeout_s}s"
    except FileNotFoundError as exc:
        detail = f"driver executable not found: {exc}"
    finally:
        if recorder is not None:
            recorder.stop()
    _write_scanned(dest / "answer.txt", answer, scan)
    _write_scanned(dest / "runner-stderr.txt", stderr_text, scan)
    _write_scanned(dest / "runner-stdout.txt", stdout_text, scan)

    api_surface = None
    if recorder is not None:
        api_surface = summarize(dest / "api-surface.jsonl", driver.disallowed_builtins)
    if detail is not None:
        verdict = "broken"
    elif exit_status != 0:
        verdict, detail = "broken", f"probe runner exited {exit_status}: {stderr_text[-400:]}"
    elif not answer.strip():
        verdict, detail = "broken", "empty answer -- an un-run canary must never read as blocked"
    elif api_surface and api_surface["disallowed_builtins_on_wire"]:
        verdict = "broken"
        detail = (f"disallowed builtin(s) {api_surface['disallowed_builtins_on_wire']} on the "
                  "wire during the canary -- measure the surface breach before egress")
    elif "NO NETWORK ACCESS" in answer:
        verdict = EGRESS_PASS
    else:
        verdict = "egress-not-verified-blocked"
        detail = ("the consumer did not report the honest-failure protocol; either egress "
                  "succeeded, the result was fabricated, or the attempt never happened. "
                  "Read answer.txt and runner-stdout.txt (the exec event stream) to tell "
                  f"which. Answer head: {answer.strip()[:200]!r}")

    record = {
        "driver": {"id": driver.id, "cli_version": driver.cli_version()},
        "model": model,
        "url": url,
        "prompt": prompt,
        "command": turn.argv,
        "env_overrides": turn.env_overrides,
        "attribution": attribution,
        "environment_state": driver.environment_state(turn),
        "started_utc": started,
        "finished_utc": _utcnow(),
        "exit_status": exit_status,
        "answer": answer,
        "api_surface": api_surface,
        "verdict": verdict,
        "detail": detail,
        "caveat": ("Effect-level observation under harness instruments: the consumer was asked "
                   "to fetch and only the exact honest-failure protocol passes. Valid per the "
                   "recorded cli_version; re-run on driver version change. Scoring of "
                   "attribution cells still reviews recorded exec events alongside the trace "
                   "(50-drivers.md)."),
    }
    _write_scanned(dest / "probe.json", json.dumps(record, indent=2) + "\n", scan)
    return record


def probe_loop_cells(manifest: Manifest, cells: list[str], out: Path, *, timeout_s: int = DEFAULT_TIMEOUT_S,
                     cache_dir: Path | None = None, drivers: dict[str, Driver] | None = None,
                     log: Callable[[str], None] = _print_log) -> dict[str, dict]:
    """Run the loop calibration probe (50-drivers.md loop #9; S12) for the named loop
    cells and nothing else: no spawn check, no scored turn. The roster eligibility
    check an operator runs before committing to a grid; artifacts per cell under
    ``out/<cell>/loop-probe/``, verdicts cached exactly as a run would cache them."""
    drivers = drivers or dict(DRIVERS)
    out = out.resolve()
    if not cells:
        raise HarnessError("no loop cells selected; the manifest defines "
                           f"{[c for c, cell in manifest.cells.items() if cell['driver'] == 'loop']}")
    unknown = [c for c in cells if c not in manifest.cells]
    if unknown:
        raise HarnessError(f"unknown cell(s) {unknown}; the manifest defines {sorted(manifest.cells)}")
    not_loop = [c for c in cells if manifest.cells[c]["driver"] != "loop"]
    if not_loop:
        raise HarnessError(f"cell(s) {not_loop} are not loop cells; the calibration probe applies to "
                           "loop cells only")
    driver = drivers["loop"]
    missing_env = [name for name in driver.required_env if not os.environ.get(name, "").strip()]
    if missing_env:
        raise HarnessError(f"driver 'loop' requires env var(s) {missing_env} and none was found. The value "
                           "reaches the consumer by process env only -- export it in the shell that runs "
                           "the harness; it appears in no artifact.")
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or (out.parent / "loop-probe-cache")
    log(f"manifest   : {manifest.sha256[:16]}  ({manifest.path})")
    log(f"probe dir  : {out}")
    driver.pre_run({c: manifest.cells[c] for c in cells}, dict.fromkeys(cells, 0), None, log)
    scan = collect_scan_set(manifest.data)
    for name in driver.required_env:
        value = os.environ.get(name, "")
        if len(value) >= 8 and value not in scan:
            scan.append(value)
    records: dict[str, dict] = {}
    for cell_name in cells:
        cell = manifest.cells[cell_name]
        gate = driver.cell_gate(cell_name, cell, out / cell_name / "loop-probe", cache_dir, log, timeout_s, scan)
        record = gate["record"]
        records[cell_name] = record
        log(f"probe      : {cell_name}  {cell['model']} @ {record['tuple']['provider']} -> {record['verdict']}"
            + ("  (cached)" if record.get("cached") else "")
            + (f"  served={record.get('served_provider')}" if record.get("served_provider") else "")
            + (f"  -- {record['detail']}" if record.get("detail") else ""))
    (out / "probe-summary.json").write_text(json.dumps(
        {"manifest": {"path": str(manifest.path), "sha256": manifest.sha256},
         "generated_utc": _utcnow(), "cells": records}, indent=2) + "\n")
    return records


def preflight(config: RunConfig) -> None:
    if type(config.precondition_retries) is not int or config.precondition_retries < 0:
        raise HarnessError("--precondition-retries must be an integer >= 0")
    if config.repeats is not None and (type(config.repeats) is not int or config.repeats < 1):
        raise HarnessError("--repeats must be an integer >= 1")
    manifest = config.manifest
    transport = manifest.server["transport"]
    if transport["type"] != "stdio":
        raise HarnessError(
            "server.transport.type 'http' is not yet supported: the harness cannot interpose "
            "trace recording on that transport, so every cell would run unattributable. "
            "Refused as instrument-broken rather than run untraced.")
    unknown_cells = [c for c in config.cells if c not in manifest.cells]
    if unknown_cells:
        raise HarnessError(f"unknown cell(s) {unknown_cells}; the manifest defines "
                           f"{sorted(manifest.cells)}")
    for cell_name in config.cells:
        driver_id = manifest.cells[cell_name]["driver"]
        if driver_id not in config.drivers:
            raise HarnessError(
                f"cell {cell_name!r} names driver {driver_id!r}, which this harness does not "
                f"provide (available: {sorted(config.drivers)}). The roster of supported "
                "drivers is an implementation property; the cell is refused, not skipped.")
        driver = config.drivers[driver_id]
        if manifest.cells[cell_name]["context"] == "crowded" and not driver.supports_sessions:
            raise HarnessError(
                f"cell {cell_name!r} is crowded but driver {driver_id!r} does not support "
                "sessions; a crowding pre-turn that cannot verifiably resume would mislabel "
                "a fresh cell as crowded. The cell is refused, not degraded.")
    if config.prompts:
        known = {p["id"] for p in manifest.prompts}
        missing = sorted(config.prompts - known)
        if missing:
            raise HarnessError(f"--prompts names unknown id(s) {missing}")
    if not config.dry_run:
        for driver_id in sorted({manifest.cells[c]["driver"] for c in config.cells}):
            driver = config.drivers[driver_id]
            if shutil.which(driver.executable) is None:
                raise HarnessError(f"driver {driver_id!r} executable {driver.executable!r} not on PATH")
            missing_env = [name for name in driver.required_env
                           if not os.environ.get(name, "").strip()]
            if missing_env:
                raise HarnessError(
                    f"driver {driver_id!r} requires env var(s) {missing_env} and none was found. "
                    "The value reaches the driver by process env only -- export it in the shell "
                    "that runs the harness; it appears in no artifact.")
        # Resolve every $secret now, so a missing export fails once, loudly, before
        # anything is spent -- not as N consumer failures that are really one unset var.
        for cell_name in config.cells:
            resolve_env_map(_server_env_spec(manifest, manifest.cells[cell_name]),
                            where=f"cells.{cell_name}.env")


def replacement_cause(meta: dict) -> str | None:
    if meta.get("breaches") or meta.get("web_activity_suspected"):
        return None
    if meta.get("precondition_unmet"):
        return meta["precondition_unmet"]["cause"]
    if meta.get("upstream_unavailable") and str(meta.get("harness_failure")).startswith("upstream_unavailable:"):
        return "upstream_unavailable"
    return None


def unavailable_slot_failures(slots: dict, results: list[dict], dead: list[str]) -> int:
    """Count unfilled unavailable slots once; zero-trace cell failure already covers them."""
    cells = {row["cell"] for row in results} - set(dead)
    return sum(slot.get("cause") == "upstream_unavailable" and slot.get("status") in {"failed", "exhausted"}
               and slot.get("cell") in cells for slot in slots.values())


def run(config: RunConfig) -> RunResult:
    # Every path handed to a driver or proxy must survive a cwd change: the consumer
    # runs in a neutral temp directory, so a relative run dir would make the config
    # paths resolve to nothing there (measured: every invocation of a live run failed
    # with "MCP config file not found" under the neutral cwd, 2026-08-31).
    config.run_dir = config.run_dir.resolve()
    preflight(config)
    manifest = config.manifest
    planned = plan_invocations(manifest, config.cells, config.groups, config.prompts)
    if not planned:
        raise HarnessError("zero invocations selected. Refusing to write a run directory "
                           "that would read as a completed suite.")
    if config.run_dir.exists() and any(config.run_dir.iterdir()):
        raise HarnessError(f"{config.run_dir} exists and is not empty. Refusing to mix two runs "
                           "in one directory -- the diff by prompt id is what gives a re-run its "
                           "meaning.")
    config.run_dir.mkdir(parents=True, exist_ok=True)
    _write_scanned(config.run_dir / "manifest-source.json", manifest.raw_bytes.decode(),
                   collect_scan_set(manifest.data))
    write_json(config.run_dir / "run-input.json", {
        "selection": {"cells": config.cells, "repeats": config.repeats,
                      "precondition_retries": config.precondition_retries,
                      "groups": sorted(config.groups) if config.groups else None,
                      "prompts": sorted(config.prompts) if config.prompts else None},
        "budget_usd": config.budget_usd, "dry_run": config.dry_run}, collect_scan_set(manifest.data))

    driver_ids = {manifest.cells[c]["driver"] for c in config.cells}
    driver_versions = ({d: None for d in driver_ids} if config.dry_run else
                       {d: config.drivers[d].cli_version() for d in sorted(driver_ids)})
    repeat_count = config.repeats or 1
    invocations_per_cell: dict[str, int] = {}
    for item in planned:
        invocations_per_cell[item.cell_name] = invocations_per_cell.get(item.cell_name, 0) + repeat_count

    # S9: the run directory is named up front, before anything can go wrong, so an
    # operator who suspects trouble knows where the artifacts are.
    log = config.log
    log(f"manifest   : {manifest.sha256[:16]}  ({manifest.path})")
    log(f"run dir    : {config.run_dir}")
    log(f"invocations: {len(planned) * repeat_count}   (one fresh process each -- never batched)"
        + ("   [DRY RUN]" if config.dry_run else ""))
    worst_attempts = repeat_count * sum(
        1 + config.precondition_retries
        for item in planned)
    log(f"attempts   : expected {len(planned) * repeat_count}; worst-case {worst_attempts} "
        f"with up to {config.precondition_retries} replacement(s) per slot")
    for driver_id in sorted(driver_ids):
        log(f"driver     : {driver_id}  [{driver_versions[driver_id] or 'not recorded: dry run'}]")

    # Family-level pre-run legibility (loop #6, S9 kin): invocation count, the labeled
    # cost estimate, the data-policy disclosure -- before the first invocation.
    pre_run_records: dict[str, dict] = {}
    if not config.dry_run:
        for driver_id in sorted(driver_ids):
            record = config.drivers[driver_id].pre_run(
                {c: manifest.cells[c] for c in config.cells}, invocations_per_cell, config.budget_usd, log)
            if record is not None:
                expected = record.get("estimate_usd_total")
                estimates = [
                    (info.get("estimate") or {}).get("usd_total")
                    for info in record.get("cells", {}).values()]
                worst = None
                if estimates and all(value is not None for value in estimates):
                    worst = round(sum(
                        info["estimate"]["usd_total"] *
                        (1 + config.precondition_retries)
                        for info in record["cells"].values()), 6)
                record["precondition_replacements"] = {
                    "retries": config.precondition_retries,
                    "expected_usd": expected, "worst_case_usd": worst}
                log(f"replacement spend ESTIMATE: expected {expected} USD (one attempt per slot); "
                    f"worst-case {worst} USD (all allowed attempts; same token assumptions)")
                pre_run_records[driver_id] = record

    # Q2 gate for drivers WITHOUT API-boundary capture: before any prompt is spent,
    # such a driver hosting a merge-gating cell runs the calibration probe (a
    # discarded invocation under the identical configuration). A non-passing probe
    # refuses the run as instrument-broken rather than producing gating results that
    # cannot be attribution-scored (documentation/10-harness.md; the probe artifacts
    # stay in the run dir as evidence either way). Capture-capable drivers skip the
    # probe: their surface is observed off the wire on every invocation, which
    # supersedes the calibration form (Q2 ruling, 455568a).
    scan = collect_scan_set(manifest.data)
    driver_probes: dict[str, dict] = {}
    if not config.dry_run:
        gating_drivers = sorted({
            manifest.cells[c]["driver"] for c in config.cells
            if manifest.cells[c]["merge_gating"]
            and not config.drivers[manifest.cells[c]["driver"]].supports_api_capture})
        for driver_id in gating_drivers:
            record = probe_builtin_surface(
                config.drivers[driver_id], config.run_dir / "driver-probe" / driver_id,
                timeout_s=config.timeout_s, tmp_base=config.tmp_base, scan=scan)
            driver_probes[driver_id] = record
            log(f"probe      : {driver_id} builtin surface -> {record['verdict']}")
            if record["verdict"] != PROBE_PASS:
                raise HarnessError(
                    f"builtin-surface probe for driver {driver_id!r} returned "
                    f"{record['verdict']!r}: {record['detail']}. Merge-gating cells are refused "
                    f"as instrument-broken; evidence in "
                    f"{config.run_dir / 'driver-probe' / driver_id / 'probe.json'}.")

    by_cell: dict[str, list[Planned]] = {}
    for item in planned:
        by_cell.setdefault(item.cell_name, []).append(item)

    results: list[dict] = []
    voided: dict[str, str] = {}
    failures = 0
    cell_gates: dict[str, dict] = {}
    consumer_limits: dict[str, str] = {}
    preconditions_unmet: dict[str, dict] = {}
    invocation_counts: dict[str, dict] = {}
    slots: dict[str, dict] = {}
    for item in planned:
        invocation_counts.setdefault(item.cell_name, {})[item.entry["id"]] = {
            "asked": repeat_count, "reached": 0, "attempts": 0,
            "attempts_unmet": 0, "attempts_unavailable": 0}
        for repetition in range(1, repeat_count + 1):
            slot_path = row_relative_path(config, item.cell_name, item.entry["group"],
                                          item.entry["id"], repetition if config.repeats is not None else None)
            slots[slot_path.as_posix()] = {"status": "not_started", "result": None, "attempts": 0,
                                          "cell": item.cell_name, "attempts_unmet": 0, "attempts_unavailable": 0}
    budget_stops: list[dict] = []
    spend_run = 0.0
    spend_cell: dict[str, float] = {}
    cache_dir = config.probe_cache_dir or (config.run_dir.parent / "loop-probe-cache")
    run_stopped = False

    def over_run_cap() -> bool:
        return config.budget_usd is not None and spend_run >= config.budget_usd

    advertised_by_cell: dict[str, list[str]] = {}
    capped_cells: set[str] = set()
    for pass_number in range(1, repeat_count + 1):
        for cell_name in config.cells:
            cell_planned = by_cell.get(cell_name, [])
            if not cell_planned or cell_name in voided or cell_name in capped_cells:
                continue
            cell = manifest.cells[cell_name]
            driver = config.drivers[cell["driver"]]
            if run_stopped:
                status = "stopped" if any(m["cell"] == cell_name for m in results) else "not started"
                log(f"cell {cell_name}: {status} -- the run-level budget cap stopped the run")
                continue
            advertised = advertised_by_cell.get(cell_name)
            # S8 (from DR-1): fail-fast instrument liveness at spawn. The SUT is spawned
            # once per cell under the invocation's own conditions (neutral cwd, cell env)
            # BEFORE any model turn; a dead or toolless server breaks the cell here,
            # with zero prompts spent, and the breach is surfaced live.
            if not config.dry_run and pass_number == 1:
                check = spawn_liveness_check(manifest, cell, config.run_dir / cell_name,
                                             scan, config.tmp_base)
                if check["ok"]:
                    advertised = list(check["advertised_tools"])
                    advertised_by_cell[cell_name] = advertised
                    log(f"cell {cell_name}: spawn check live "
                        f"({len(check['advertised_tools'])} tool(s) advertised); "
                        f"{len(cell_planned)} prompt(s)")
                else:
                    reason = (f"instrument liveness failed at spawn (S8): {check['reason']}. "
                              "No model turn was spent. See spawn-check.json (stderr tail "
                              "included) beside CELL-VOID.json.")
                    write_cell_void(config.run_dir, cell_name, reason)
                    voided[cell_name] = reason
                    failures += 1
                    log(f"cell {cell_name}: BROKEN at spawn -- {check['reason']}")
                    log(f"  {len(cell_planned)} prompt(s) skipped without spending model turns; "
                        f"evidence: {config.run_dir / cell_name / 'spawn-check.json'}")
                    continue
                # The driver family's own cell gate, after the spawn check and before any
                # model turn: the loop driver's calibration probe (S12; loop #9), cached
                # per (model, provider, scaffold, driver version). A non-pass BREAKS the
                # cell with zero prompts spent.
                gate = driver.cell_gate(cell_name, cell, config.run_dir / cell_name / "loop-probe",
                                        cache_dir, log, config.timeout_s, scan)
                if gate is not None:
                    cell_gates[cell_name] = gate["record"]
                    gate_cost = float(gate.get("cost_usd") or 0.0)
                    spend_run += gate_cost
                    spend_cell[cell_name] = spend_cell.get(cell_name, 0.0) + gate_cost
                    record = gate["record"]
                    log(f"cell {cell_name}: calibration probe -> {record.get('verdict')}"
                        + ("  (cached)" if record.get("cached") else "")
                        + (f"  served={record.get('served_provider')}" if record.get("served_provider") else "")
                        + (f"  ${gate_cost:.6f}" if gate_cost else ""))
                    if not gate["ok"]:
                        reason = f"instrument-broken before any model turn: {gate['reason']}"
                        write_cell_void(config.run_dir, cell_name, reason)
                        voided[cell_name] = reason
                        failures += 1
                        log(f"cell {cell_name}: BROKEN -- {gate['reason']}")
                        log(f"  {len(cell_planned)} prompt(s) skipped without spending model turns.")
                        continue
            cell_cap = cell.get("budget_usd")
            for index, item in enumerate(cell_planned):
                entry = item.entry
                remaining = len(cell_planned) * (repeat_count - pass_number + 1) - index
                slot_name = row_relative_path(
                    config, cell_name, entry["group"], entry["id"],
                    pass_number if config.repeats is not None else None).as_posix()
                slot = slots[slot_name]
                cause = None
                for attempt in range(1, config.precondition_retries + 2):
                    if over_run_cap():
                        stop = {"scope": "run", "cap_usd": config.budget_usd, "spent_usd": round(spend_run, 8),
                                "at": f"{cell_name}/{entry['id']}", "skipped_in_cell": remaining}
                        if attempt > 1:
                            stop.update(slot=slot_name, attempt=attempt)
                        slot["status"] = "budget_stopped"
                        budget_stops.append(stop)
                        run_stopped = True
                        log(f"BUDGET STOP: run spend ${spend_run:.6f} reached the run cap "
                            f"${config.budget_usd} before {cell_name}/{entry['id']}; the run stops "
                            f"cleanly, completed cells stand, {remaining} prompt(s) in this cell not run.")
                        break
                    if cell_cap is not None and spend_cell.get(cell_name, 0.0) >= cell_cap:
                        stop = {"scope": "cell", "cell": cell_name, "cap_usd": cell_cap,
                                "spent_usd": round(spend_cell[cell_name], 8),
                                "at": f"{cell_name}/{entry['id']}", "skipped_in_cell": remaining}
                        if attempt > 1:
                            stop.update(slot=slot_name, attempt=attempt)
                        slot["status"] = "budget_stopped"
                        budget_stops.append(stop)
                        capped_cells.add(cell_name)
                        log(f"BUDGET STOP: cell {cell_name} spend ${spend_cell[cell_name]:.6f} reached its "
                            f"cap ${cell_cap} before {entry['id']}; {remaining} prompt(s) in this cell "
                            "not run. Completed invocations stand.")
                        break
                    if attempt > 1:
                        log(f"REPLACEMENT: {cause}: {slot_name} attempt {attempt}/"
                            f"{config.precondition_retries + 1}, whole fresh invocation")
                    repeat_label = (f" r{pass_number:0{max(2, len(str(repeat_count)))}d}/{repeat_count}"
                                    if config.repeats is not None else "")
                    log(f"  -> {cell_name}/{entry['id']}{repeat_label}"
                        + ("  [outside cell groups]" if item.outside_cell_groups else ""))
                    meta = run_one(config, item, driver_versions, advertised_tools=advertised,
                                   repetition=pass_number if config.repeats is not None else None, attempt=attempt)
                    results.append(meta)
                    path = meta_relative_path(config, meta).as_posix()
                    counts = invocation_counts[cell_name][entry["id"]]
                    counts["attempts"] += 1
                    reached = meta.get("scored_turn_reached", not meta["harness_failure"])
                    counts["reached"] += int(reached)
                    slot["attempts"] = attempt
                    slot["status"] = "scored" if reached and not meta["harness_failure"] else "failed"
                    if reached:
                        slot["result"] = path
                    if meta.get("spend_usd") is not None:
                        spend_run += float(meta["spend_usd"])
                        spend_cell[cell_name] = spend_cell.get(cell_name, 0.0) + float(meta["spend_usd"])
                    flag = ""
                    eligible = replacement_cause(meta)
                    slot.pop("cause", None)
                    for counter, present in (("attempts_unmet", bool(meta.get("precondition_unmet"))),
                                             ("attempts_unavailable", eligible == "upstream_unavailable")):
                        counts[counter] += int(present)
                        slot[counter] += int(present)
                    if meta.get("precondition_unmet"):
                        cause = meta["precondition_unmet"]["cause"]
                        preconditions_unmet[path] = {"slot": slot_name, "attempt": attempt, "cause": cause}
                        slot["status"] = "replacement_disabled" if config.precondition_retries == 0 else "exhausted"
                        flag = "  PRECONDITION UNMET"
                    elif eligible == "upstream_unavailable":
                        cause = "upstream_unavailable"
                        slot["cause"] = cause
                        slot["status"] = "exhausted" if config.precondition_retries else "failed"
                        flag = "  UPSTREAM UNAVAILABLE"
                    elif meta["harness_failure"]:
                        failures += 1
                        flag = "  HARNESS FAILURE"
                    elif meta.get("consumer_limit"):
                        limit_label = (meta_relative_path(config, meta).as_posix() if config.repeats is not None
                                       else f"{cell_name}/{entry['id']}")
                        consumer_limits[limit_label] = meta["consumer_limit"]["cause"]
                        outcome_label = ("CONSUMER OUTCOME" if meta["consumer_limit"]["cause"] == "null_final_content"
                                         else "CONSUMER LIMIT")
                        flag = (f"  {outcome_label} ({meta['consumer_limit']['cause']}): no answer -- "
                                "a consumer outcome, not a harness failure (S13)")
                    family = meta.get(driver.family) if driver.family != "product" else None
                    loop_note = ""
                    if family:
                        served = ",".join(f"{k}x{v}" for k, v in (family.get("served_providers") or {}).items())
                        loop_note = f"  served={served or 'none'} ${float(meta.get('spend_usd') or 0):.6f}"
                    log(f"     {entry['id']:6s}{repeat_label} {meta['duration_s']:6.1f}s  "
                        f"{meta['trace_records']:>3} trace record(s)  "
                        f"{meta['answer_chars']:>6} chars{loop_note}{flag}")
                    if meta["harness_failure"]:
                        log(meta["harness_failure"])
                    # Attribution breach (a disallowed builtin on the wire, web-tool
                    # activity in the driver's event streams, a wire surface that differs
                    # from the cell's, a served-provider mismatch, a strict-routing
                    # refusal): the row is not tool-attributable and the CELL is BROKEN --
                    # refusal is hard, not best-effort (50-drivers.md #5; the measured
                    # failure class includes a web tool fabricating a fetch result
                    # presented as retrieved).
                    breach = list(meta.get("web_activity_suspected") or [])
                    breach += list(meta.get("breaches") or [])
                    if breach:
                        reason = (f"attribution breach in {entry['id']}: {breach[0]} -- "
                                  "the environment the configuration claims does not hold; no row of "
                                  "this cell is tool-attributable.")
                        write_cell_void(config.run_dir, cell_name, reason)
                        voided[cell_name] = reason
                        remaining = len(cell_planned) * (repeat_count - pass_number + 1) - index - 1
                        log(f"cell {cell_name}: BROKEN -- {reason}")
                        if remaining:
                            log(f"  {remaining} remaining prompt(s) skipped without spending "
                                "model turns.")
                        break

                    if not eligible or attempt > config.precondition_retries:
                        break
                if run_stopped or cell_name in capped_cells or cell_name in voided:
                    break

    dead = zero_trace_cells(results, config.dry_run)
    failures += len(dead) + unavailable_slot_failures(slots, results, dead)
    for cell_name in dead:
        reason = ("every invocation in this cell recorded zero trace records without sufficient "
                  "row-local liveness evidence. BROKEN, never an abstention or a clean run.")
        write_cell_void(config.run_dir, cell_name, reason)
        for meta in results:
            if meta["cell"] != cell_name:
                continue
            meta["cell_void"] = reason
            row_path = config.run_dir / meta_relative_path(config, meta) / "meta.json"
            row_path.write_text(json.dumps(meta, indent=2) + "\n")
    for cell_name in dead:
        log(f"cell {cell_name}: BROKEN -- zero trace records across every invocation; "
            "an empty run, not a clean one. Do not score it.")

    # S11 marks per cell, for every artifact that names a cell: run-manifest and the
    # checks report carry the family, scaffold with hash, pin or unpinned, the
    # reproducibility mark, the served-provider set observed, and the spend.
    cell_marks: dict[str, dict] = {}
    for cell_name in config.cells:
        cell = manifest.cells[cell_name]
        marks = config.drivers[cell["driver"]].cell_marks(cell)
        # Distinct answers per prompt within the cell (WO-3, keyed by prompt per the
        # WO-4 addendum): the determinism question is not loop-specific, so every cell
        # carries it. The across-prompts total is labeled as exactly that.
        marks["answers"] = answers_by_prompt(results, cell_name)
        marks["answers_across_prompts"] = answers_across_prompts(marks["answers"])
        if "scaffold" not in marks:
            cell_marks[cell_name] = marks  # product family: the family mark plus answers
            continue
        served: dict[str, int] = {}
        family = config.drivers[cell["driver"]].family
        for meta in results:
            if meta["cell"] == cell_name:
                for name, n in ((meta.get(family) or {}).get("served_providers") or {}).items():
                    served[name] = served.get(name, 0) + n
        cell_marks[cell_name] = {**marks, "served_providers": served,
                                 "spend_usd": round(spend_cell.get(cell_name, 0.0), 8),
                                 "probe": ({k: cell_gates[cell_name].get(k) for k in
                                            ("verdict", "cached", "probed_at", "served_provider", "cache_path")}
                                           if cell_name in cell_gates else None)}
        if marks.get("provider_pin") != "unpinned":
            rows = [m for m in results if m["cell"] == cell_name]
            verified = {v for m in rows for v in ([(m.get(family) or {}).get("provider_verified")]
                                                  if isinstance((m.get(family) or {}).get("provider_verified"), str)
                                                  else ((m.get(family) or {}).get("provider_verified") or []))}
            cell_marks[cell_name]["provider_verified"] = sorted(verified) or None

    checks_report: list[dict] = []
    measurement_summary: dict[str, dict] = {}

    run_manifest = {
        "harness_version": __version__,
        "manifest": {"path": str(manifest.path), "sha256": manifest.sha256},
        "generated_utc": _utcnow(),
        "selection": {"cells": config.cells, "repeats": config.repeats,
                      "precondition_retries": config.precondition_retries,
                      "groups": sorted(config.groups) if config.groups else None,
                      "prompts": sorted(config.prompts) if config.prompts else None},
        "driver_versions": driver_versions,
        "driver_probes": {d: {"verdict": r["verdict"],
                              "builtins_found": r["builtins_found"],
                              "path": f"driver-probe/{d}/probe.json"}
                          for d, r in driver_probes.items()},
        "cells": {c: manifest.cells[c] for c in config.cells},
        "fixtures": manifest.fixtures,
        "dry_run": config.dry_run,
        "voided_cells": voided,
        "zero_trace_cell_failures": dead,
        "checks": {c["id"]: c["outcome"] for c in checks_report},
        # Row measurements recorded per row (meta.json -> measurements.<name>); this is
        # a count of what was recorded, never an outcome.
        "measurements": measurement_summary,
        # Family-level records: the pre-run legibility output (estimate, disclosure),
        # per-cell gates (the loop calibration probe), and the S11 marks per cell with
        # the served-provider set and spend actually observed.
        "pre_run": pre_run_records,
        "cell_gates": cell_gates,
        # S11 family marks for every cell ("product" carries the family alone).
        "cell_marks": cell_marks,
        "loop_cells": {c: m for c, m in cell_marks.items() if m.get("driver_family") == "loop"},
        # S13: answerless consumer outcomes, counted beside pass and fail, never broken.
        "consumer_limits": consumer_limits,
        "preconditions_unmet": preconditions_unmet,
        "invocation_counts": invocation_counts,
        "slots": slots,
        # Spend and caps: the run-level outcome the budget stop is (loop #6).
        "budget": {"run_cap_usd": config.budget_usd,
                   "spent_usd": round(spend_run, 8),
                   "per_cell_spent_usd": {c: round(v, 8) for c, v in spend_cell.items()},
                   "stops": budget_stops,
                   "run_stopped_by_budget": run_stopped},
        "failures": failures,
        "results": results,
    }
    checks_report = finish_reports(
        config.run_dir, run_manifest, manifest,
        {meta_relative_path(config, meta).as_posix(): meta for meta in results},
        update_rows=True, log=log)
    failures = run_manifest["failures"]

    if budget_stops:
        for stop in budget_stops:
            log(f"budget stop: {stop['scope']} cap ${stop['cap_usd']} reached "
                f"(spent ${stop['spent_usd']:.6f}) at {stop['at']}")
    if spend_run or config.budget_usd is not None:
        log(f"spend      : ${spend_run:.6f} actual (summed usage.cost)"
            + (f"  of run cap ${config.budget_usd}" if config.budget_usd is not None else ""))

    if preconditions_unmet:
        log(f"preconditions unmet: {len(preconditions_unmet)} (unscoreable, counted apart from harness failures)")
    for cell_name, prompts in invocation_counts.items():
        for prompt, counts in prompts.items():
            log(f"invocations {cell_name}/{prompt}: asked {counts['asked']}, "
                f"reached {counts['reached']}, attempts {counts['attempts']}, "
                f"unmet {counts['attempts_unmet']}, unavailable {counts['attempts_unavailable']}")
    if consumer_limits:
        log(f"consumer limits: {len(consumer_limits)}  (answerless consumer outcomes, S13 -- scored as a "
            f"failure to answer, NOT harness failures): "
            + ", ".join(f"{k} [{v}]" for k, v in consumer_limits.items()))
    log(f"harness failures: {failures}  (these are NOT consumer results)")
    log("This harness does not score. Pass/fail against the pinned criteria in each "
        "meta.json is a human/spec-session judgment.")

    return RunResult(run_dir=config.run_dir, results=results, failures=failures,
                     zero_trace_cells=dead, checks_report=checks_report,
                     driver_probes=driver_probes, voided_cells=voided,
                     budget_stops=budget_stops, spend_usd=round(spend_run, 8),
                     consumer_limits=consumer_limits, preconditions_unmet=preconditions_unmet,
                     invocation_counts=invocation_counts, slots=slots)
