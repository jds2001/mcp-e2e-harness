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

from . import __version__, crowding
from . import checks as checks_mod
from .api_capture import ApiSurfaceRecorder, summarize
from .drivers import DRIVERS, Driver
from .drivers.base import TurnContext
from .manifest import Manifest
from .mcp_client import MCPClientError, StdioMCPClient
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
    timeout_s: int = DEFAULT_TIMEOUT_S
    drivers: dict[str, Driver] = field(default_factory=lambda: dict(DRIVERS))
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

    # Drivers that sanitize spawned-MCP-server environments (codex) get the manifest's
    # $secret values to the proxy via a transient 0600 file outside the run tree --
    # process-env only, never an artifact (50-drivers.md, codex #6).
    secrets_file: Path | None = None
    if driver.sanitizes_mcp_env and not config.dry_run:
        secrets_file = _write_secrets_file(_server_env_spec(manifest, cell), config.run_dir)

    entries = {manifest.server["name"]: _proxy_entry(server_config, dest, "", cell,
                                                     secrets_file=secrets_file)}
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
                    extra_server_names=extra_names, dest=dest, api_base_url=api_base_url)

    started = _utcnow()
    t0 = time.perf_counter()
    harness_failure: str | None = None
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
        finally:
            if recorder is not None:
                recorder.stop()
            if secrets_file is not None:
                secrets_file.unlink(missing_ok=True)
        _write_scanned(dest / "runner-stderr.txt", stderr_text, scan)
        _write_scanned(dest / "answer.txt", answer, scan)
        if turn.answer_from != "stdout":
            # The driver's stdout is its event stream (not the answer): keep it -- it
            # is where codex reports MCP startup failures and web-tool events.
            _write_scanned(dest / "runner-stdout.txt", stdout_text, scan)

    # Effect-level web-activity scan over the driver's EVENT streams (never answer
    # content -- an answer saying "I cannot search the web" must not read as web
    # activity): stderr always, stdout only when the answer travels by file. Any hit
    # is an instrument breach (50-drivers.md, codex #5).
    if driver.web_event_markers and not config.dry_run:
        streams = [stderr_text] + ([stdout_text] if turn.answer_from != "stdout" else [])
        lowered = [s.casefold() for s in streams]
        web_activity = [m for m in driver.web_event_markers
                        if any(m in s for s in lowered)]
        if web_activity and harness_failure is None:
            harness_failure = (f"web-tool activity suspected in the driver's event streams "
                               f"({','.join(web_activity)}) -- instrument breach; claims not "
                               "tool-attributable")

    api_surface: dict | None = None
    if recorder is not None:
        api_surface = summarize(dest / "api-surface.jsonl", driver.disallowed_builtins) or {
            "requests_recorded": 0, "requests_with_tools": 0,
            "tool_names_union": [], "disallowed_builtins_on_wire": []}
        api_surface["unparseable_requests"] = recorder.unrecorded_requests
        api_surface["unparseable_encodings"] = sorted(recorder.unrecorded_encodings)
        api_surface["upstream_forward_errors"] = recorder.forward_errors
        if harness_failure is None:
            if api_surface["disallowed_builtins_on_wire"]:
                # The channel the argv claims closed is open at the wire: an instrument
                # breach, not a consumer behavior; the row's claims are not
                # attribution-scoreable (the ancestor's F30 shape).
                harness_failure = (
                    "disallowed builtin(s) "
                    f"{api_surface['disallowed_builtins_on_wire']} observed in the tools array "
                    "sent to the model -- instrument breach; claims not tool-attributable")
            elif api_surface["requests_recorded"] == 0:
                # An answer with zero recorded model requests means the driver either
                # did not route through the capture or sent nothing the recorder could
                # read; unverified must never read as verified. A tool-less request is
                # recorded (tool_names null) and does NOT trip this: a request offering
                # no tools offers no builtins.
                detail = (f" ({api_surface['unparseable_requests']} request(s) arrived but "
                          f"could not be parsed; encodings seen: "
                          f"{api_surface['unparseable_encodings']})"
                          if api_surface["unparseable_requests"] else "")
                harness_failure = (
                    "API surface capture recorded zero readable model requests: builtin "
                    f"absence is unverified for this invocation{detail}")

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

    # S9: the run directory is named up front, before anything can go wrong, so an
    # operator who suspects trouble knows where the artifacts are.
    log = config.log
    log(f"manifest   : {manifest.sha256[:16]}  ({manifest.path})")
    log(f"run dir    : {config.run_dir}")
    log(f"invocations: {len(planned)}   (one fresh process each -- never batched)"
        + ("   [DRY RUN]" if config.dry_run else ""))
    for driver_id in sorted(driver_ids):
        log(f"driver     : {driver_id}  [{driver_versions[driver_id] or 'not recorded: dry run'}]")

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
    for cell_name in config.cells:
        cell_planned = by_cell.get(cell_name, [])
        if not cell_planned:
            continue
        cell = manifest.cells[cell_name]
        # S8 (from DR-1): fail-fast instrument liveness at spawn. The SUT is spawned
        # once per cell under the invocation's own conditions (neutral cwd, cell env)
        # BEFORE any model turn; a dead or toolless server breaks the cell here,
        # with zero prompts spent, and the breach is surfaced live.
        if not config.dry_run:
            check = spawn_liveness_check(manifest, cell, config.run_dir / cell_name,
                                         scan, config.tmp_base)
            if check["ok"]:
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
        for index, item in enumerate(cell_planned):
            entry = item.entry
            log(f"  -> {cell_name}/{entry['id']}"
                + ("  [outside cell groups]" if item.outside_cell_groups else ""))
            meta = run_one(config, item, driver_versions)
            results.append(meta)
            flag = ""
            if meta["harness_failure"]:
                failures += 1
                flag = f"  HARNESS FAILURE: {meta['harness_failure'][:90]}"
            log(f"     {entry['id']:6s} {meta['duration_s']:6.1f}s  "
                f"{meta['trace_records']:>3} trace record(s)  "
                f"{meta['answer_chars']:>6} chars{flag}")
            # Attribution breach (a disallowed builtin on the wire, or web-tool
            # activity in the driver's event streams): the row is not
            # tool-attributable and the CELL is BROKEN -- refusal is hard, not
            # best-effort (50-drivers.md #5; the measured failure class includes a
            # web tool fabricating a fetch result presented as retrieved).
            breach = list(meta.get("web_activity_suspected") or [])
            breach += (meta.get("api_surface") or {}).get("disallowed_builtins_on_wire") or []
            if breach:
                reason = (f"attribution breach in {entry['id']}: {sorted(set(breach))} -- "
                          "the channel the configuration claims closed is open; no row of "
                          "this cell is tool-attributable.")
                write_cell_void(config.run_dir, cell_name, reason)
                voided[cell_name] = reason
                remaining = len(cell_planned) - index - 1
                log(f"cell {cell_name}: BROKEN -- {reason}")
                if remaining:
                    log(f"  {remaining} remaining prompt(s) skipped without spending "
                        "model turns.")
                break

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
    for cell_name in dead:
        log(f"cell {cell_name}: BROKEN -- zero trace records across every invocation; "
            "an empty run, not a clean one. Do not score it.")

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
        for check in checks_report:
            log(f"check      : {check['id']}: {check['outcome']}  (matched {check['matched']})")

    log(f"harness failures: {failures}  (these are NOT consumer results)")
    log("This harness does not score. Pass/fail against the pinned criteria in each "
        "meta.json is a human/spec-session judgment.")

    run_manifest = {
        "harness_version": __version__,
        "manifest": {"path": str(manifest.path), "sha256": manifest.sha256},
        "generated_utc": _utcnow(),
        "selection": {"cells": config.cells,
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
        "failures": failures,
        "results": results,
    }
    _write_scanned(config.run_dir / "run-manifest.json",
                   json.dumps(run_manifest, indent=2) + "\n", scan)
    return RunResult(run_dir=config.run_dir, results=results, failures=failures,
                     zero_trace_cells=dead, checks_report=checks_report,
                     driver_probes=driver_probes, voided_cells=voided)
