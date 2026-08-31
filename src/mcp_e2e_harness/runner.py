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

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, crowding
from . import checks as checks_mod
from .drivers import DRIVERS, Driver
from .drivers.base import TurnContext
from .manifest import Manifest
from .mcp_client import MCPClientError, StdioMCPClient
from .secrets import assert_clean, collect_scan_set, resolve_env_map

DEFAULT_TIMEOUT_S = 900
CONTEXT_LEAK_MARKERS = (".git", "CLAUDE.md", "AGENTS.md")


class HarnessError(RuntimeError):
    """A configuration or instrument problem that must halt before results exist."""


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
    timeout_s: int = DEFAULT_TIMEOUT_S
    drivers: dict[str, Driver] = field(default_factory=lambda: dict(DRIVERS))
    # For tests: where neutral cwds are created (must itself be leak-free).
    tmp_base: Path | None = None


@dataclass
class RunResult:
    run_dir: Path
    results: list[dict]
    failures: int
    zero_trace_cells: list[str]
    checks_report: list[dict]


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


def cell_id_of(cell_name: str, cell: dict) -> str:
    surface = cell["tool_surface"]
    surface_tag = "full" if surface == "full" else "surface:" + "+".join(surface)
    return f"{cell['driver']}/{cell['model']}/{cell['context']}/{surface_tag}"


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


def _proxy_entry(server_config: Path, dest: Path, prefix: str, cell: dict) -> dict:
    args = ["-m", "mcp_e2e_harness.proxy",
            "--server-config", str(server_config),
            "--trace-file", str(dest / f"{prefix}trace.jsonl"),
            "--tools-file", str(dest / f"{prefix}available-tools.json"),
            "--meta-file", str(dest / f"{prefix}proxy-meta.json")]
    if cell["tool_surface"] != "full":
        args += ["--allow-tools", ",".join(cell["tool_surface"])]
    return {"command": sys.executable, "args": args}


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


def _crowding_preturn(driver: Driver, ctx_base: dict, procedure: crowding.CrowdingProcedure,
                      dest: Path, cwd: Path, timeout_s: int, scan: list[str],
                      session_id: str, crowd_config: Path) -> dict:
    """Run the crowding procedure's opening turn so the scored prompt lands mid-task."""
    ctx = TurnContext(**ctx_base, mcp_config_path=crowd_config,
                      session=("open", session_id), prompt=procedure.opening_prompt)
    turn = driver.build_turn(ctx)
    driver.attribution_record(turn.argv)
    proc = subprocess.run(turn.argv, cwd=cwd, input=turn.stdin_text,
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
    _write_scanned(dest / "crowding.json", json.dumps(record, indent=2) + "\n", scan)
    if proc.returncode != 0:
        raise HarnessError(
            f"crowding pre-turn exited {proc.returncode}; the consumer is NOT mid-task, so "
            "running the scored prompt would mislabel a fresh cell as crowded. See "
            f"{dest / 'crowding.json'}.")
    return record


def run_one(config: RunConfig, planned: Planned, driver_versions: dict[str, str]) -> dict:
    manifest = config.manifest
    entry, cell_name, cell = planned.entry, planned.cell_name, planned.cell
    driver = config.drivers[cell["driver"]]
    dest = config.run_dir / cell_name / entry["group"] / entry["id"]
    dest.mkdir(parents=True, exist_ok=True)
    scan = collect_scan_set(manifest.data)

    cwd = make_neutral_cwd(entry["id"], config.tmp_base)
    prompt_text = resolve_prompt(entry, cell)
    server_config = _write_server_config(dest, manifest, cell)
    assert_clean(server_config.read_text(), scan, str(server_config))

    crowded = cell["context"] == "crowded"
    procedure = crowding.get_procedure(cell["crowding"]["procedure"]) if crowded else None

    entries = {manifest.server["name"]: _proxy_entry(server_config, dest, "", cell)}
    extra_names: tuple[str, ...] = ()
    if procedure is not None:
        entries[procedure.server_name] = _distractor_entry(procedure, dest)
        extra_names = (procedure.server_name,)
    driver_config = driver.write_driver_config(dest, entries)
    assert_clean(driver_config.read_text(), scan, str(driver_config))

    ctx_base = dict(model=cell["model"], knobs=cell["knobs"],
                    server_name=manifest.server["name"], tool_surface=cell["tool_surface"],
                    extra_server_names=extra_names, dest=dest)

    started = _utcnow()
    t0 = time.perf_counter()
    harness_failure: str | None = None
    exit_status = -1
    answer = ""
    stderr_text = ""
    setup_records: list[dict] = []
    crowding_record: dict | None = None
    session = ("single", "")

    # Build the scored turn up front so argv-level assertions run even on --dry-run.
    if crowded:
        session_id = str(uuid.uuid4())
        crowd_entries = dict(entries)
        crowd_entries[manifest.server["name"]] = _proxy_entry(server_config, dest, "crowding-", cell)
        crowd_config = driver.write_driver_config(dest, crowd_entries)
        # Both turns of a crowded invocation share one config file name per driver, so
        # give the pre-turn config its own path.
        crowd_config = crowd_config.rename(dest / "mcp-config-crowding.json")
        driver_config = driver.write_driver_config(dest, entries)
        session = ("resume", session_id)
    scored_ctx = TurnContext(**ctx_base, mcp_config_path=driver_config,
                             session=session, prompt=prompt_text)
    turn = driver.build_turn(scored_ctx)
    attribution = driver.attribution_record(turn.argv)
    assert_clean("\n".join(turn.argv), scan, "runner argv")

    if config.dry_run:
        exit_status, answer = 0, "[dry-run: no model was called]"
    else:
        try:
            if cell.get("setup"):
                setup_records = _run_setup(manifest, cell, dest, scan)
            if crowded and procedure is not None:
                crowding_record = _crowding_preturn(
                    driver, ctx_base, procedure, dest, cwd, config.timeout_s, scan,
                    session_id, dest / "mcp-config-crowding.json")
            proc = subprocess.run(turn.argv, cwd=cwd, input=turn.stdin_text,
                                  capture_output=True, text=True, timeout=config.timeout_s)
            exit_status = proc.returncode
            stderr_text = proc.stderr
            if turn.answer_from == "stdout":
                answer = proc.stdout
            elif turn.answer_path and turn.answer_path.exists():
                answer = turn.answer_path.read_text()
            if exit_status != 0:
                harness_failure = f"runner exited {exit_status}: {stderr_text[-800:]}"
            elif not answer.strip():
                # A crashed invocation or an empty answer must never be readable as a
                # consumer that chose not to call anything -- an errored scan must not
                # look like one that found nothing.
                harness_failure = "empty answer with exit 0 -- harness failure, NOT a consumer result"
        except subprocess.TimeoutExpired as exc:
            harness_failure = f"timeout after {config.timeout_s}s -- harness failure, NOT a consumer result"
            partial = exc.stderr
            stderr_text = (partial.decode(errors="replace")
                           if isinstance(partial, bytes) else (partial or ""))
        except FileNotFoundError as exc:
            harness_failure = f"runner not found: {exc}"
        except (HarnessError, MCPClientError) as exc:
            harness_failure = str(exc)
        _write_scanned(dest / "runner-stderr.txt", stderr_text, scan)
        _write_scanned(dest / "answer.txt", answer, scan)

    duration = round(time.perf_counter() - t0, 2)
    records, parse_errors = _read_trace(dest / "trace.jsonl")
    if (dest / "trace.jsonl").exists():
        assert_clean((dest / "trace.jsonl").read_text(errors="replace"), scan, str(dest / "trace.jsonl"))
    if parse_errors and not harness_failure:
        harness_failure = (f"{parse_errors} unparseable trace line(s) -- instrument defect; "
                           "fix the instrument before any disposition")

    tools_path = dest / "available-tools.json"
    recorded_surface = json.loads(tools_path.read_text()) if tools_path.exists() else None

    meta = {
        "prompt_id": entry["id"],
        "group": entry["group"],
        "cell": cell_name,
        "cell_id": cell_id_of(cell_name, cell),
        "driver": {"id": cell["driver"], "cli_version": driver_versions.get(cell["driver"])},
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
        "trace_records": len(records),
        "trace_parse_errors": parse_errors,
        "tool_calls": [r.get("tool", "?") for r in records],
        "answer_chars": len(answer),
        "command": turn.argv,
        "cwd": str(cwd),
        "recorded_tool_surface": recorded_surface,
        "attribution": attribution,
        "setup": setup_records,
        "crowding": ({"procedure": procedure.name, "version": procedure.version,
                      "content_hash": procedure.content_hash(),
                      "collision_review": cell["crowding"]["collision_review"],
                      "preturn_ok": crowding_record is not None}
                     if procedure is not None else None),
        # Criteria travel WITH the result so a scorer never has to reconstruct them,
        # and so editing one after seeing a result is visible in the diff.
        "criteria": {k: entry.get(k) for k in
                     ("title", "pass", "fail", "rubric", "watch", "grounding", "sourcing")},
    }
    _write_scanned(dest / "meta.json", json.dumps(meta, indent=2) + "\n", scan)
    return meta


def zero_trace_cells(results: list[dict], dry_run: bool) -> list[str]:
    """Cells where every invocation recorded zero trace records: BROKEN instruments.

    A single-prompt cell with an empty trace is the canonical case; it is never an
    abstention, a pass, or a vacuous result (Layer-1 instrument liveness).
    """
    if dry_run:
        return []
    totals: dict[str, int] = {}
    for meta in results:
        totals[meta["cell"]] = totals.get(meta["cell"], 0) + meta["trace_records"]
    return sorted(cell for cell, total in totals.items() if total == 0)


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


def preflight(config: RunConfig) -> None:
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
    if config.prompts:
        known = {p["id"] for p in manifest.prompts}
        missing = sorted(config.prompts - known)
        if missing:
            raise HarnessError(f"--prompts names unknown id(s) {missing}")
    if not config.dry_run:
        for driver_id in {manifest.cells[c]["driver"] for c in config.cells}:
            executable = config.drivers[driver_id].executable
            if shutil.which(executable) is None:
                raise HarnessError(f"driver {driver_id!r} executable {executable!r} not on PATH")
        # Resolve every $secret now, so a missing export fails once, loudly, before
        # anything is spent -- not as N consumer failures that are really one unset var.
        resolve_env_map(transport.get("env") or {}, where="server.transport.env")
        for cell_name in config.cells:
            resolve_env_map(manifest.cells[cell_name].get("env") or {},
                            where=f"cells.{cell_name}.env")


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

    driver_ids = {manifest.cells[c]["driver"] for c in config.cells}
    driver_versions = ({d: None for d in driver_ids} if config.dry_run else
                       {d: config.drivers[d].cli_version() for d in sorted(driver_ids)})

    results: list[dict] = []
    failures = 0
    for item in planned:
        meta = run_one(config, item, driver_versions)
        results.append(meta)
        if meta["harness_failure"]:
            failures += 1

    dead = zero_trace_cells(results, config.dry_run)
    failures += len(dead)
    for cell_name in dead:
        reason = ("every invocation in this cell recorded zero trace records: the instrument "
                  "never ran. BROKEN, never an abstention or a clean run.")
        write_cell_void(config.run_dir, cell_name, reason)
        for meta in results:
            if meta["cell"] != cell_name:
                continue
            row_path = config.run_dir / meta["cell"] / meta["group"] / meta["prompt_id"] / "meta.json"
            row = json.loads(row_path.read_text())
            row["cell_void"] = reason
            row_path.write_text(json.dumps(row, indent=2) + "\n")

    checks_report: list[dict] = []
    if manifest.checks and not config.dry_run:
        records_by_invocation = {}
        for meta in results:
            trace_path = config.run_dir / meta["cell"] / meta["group"] / meta["prompt_id"] / "trace.jsonl"
            records, _ = _read_trace(trace_path)
            records_by_invocation[f"{meta['cell']}/{meta['group']}/{meta['prompt_id']}"] = records
        checks_report = checks_mod.evaluate_checks(manifest.checks, records_by_invocation)
        failures += sum(1 for c in checks_report if c["outcome"] == "error")
        (config.run_dir / "checks-report.json").write_text(
            json.dumps(checks_report, indent=2) + "\n")

    run_manifest = {
        "harness_version": __version__,
        "manifest": {"path": str(manifest.path), "sha256": manifest.sha256},
        "generated_utc": _utcnow(),
        "selection": {"cells": config.cells,
                      "groups": sorted(config.groups) if config.groups else None,
                      "prompts": sorted(config.prompts) if config.prompts else None},
        "driver_versions": driver_versions,
        "cells": {c: manifest.cells[c] for c in config.cells},
        "fixtures": manifest.fixtures,
        "dry_run": config.dry_run,
        "zero_trace_cell_failures": dead,
        "checks": {c["id"]: c["outcome"] for c in checks_report},
        "failures": failures,
        "results": results,
    }
    scan = collect_scan_set(manifest.data)
    _write_scanned(config.run_dir / "run-manifest.json",
                   json.dumps(run_manifest, indent=2) + "\n", scan)
    return RunResult(run_dir=config.run_dir, results=results, failures=failures,
                     zero_trace_cells=dead, checks_report=checks_report)
