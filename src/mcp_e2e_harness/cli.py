"""CLI for the harness.

    mcp-e2e validate --manifest suite/manifest.json
    mcp-e2e run --manifest suite/manifest.json [--run-dir runs/X] [--cells a,b]
                [--groups A,B] [--prompts A1,B2] [--dry-run] [--timeout 900]
                [--budget-usd 2.00] [--loop-probe-cache DIR]

The harness executes and records; it never scores. Pass/fail against the pinned
criteria in each meta.json is a human/spec-session judgment, recorded beside -- never
instead of -- the raw artifacts.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .checks import lint_check
from .drivers import DRIVERS
from .interview import interview
from .loop_consumer import LoopError
from .manifest import Manifest, ManifestError, load_manifest
from .reporting import rebuild
from .runner import (
    DEFAULT_TIMEOUT_S,
    EGRESS_PASS,
    EGRESS_PROBE_URL,
    PROBE_PASS,
    HarnessError,
    RunConfig,
    probe_builtin_surface,
    probe_egress,
    probe_loop_cells,
    run,
)
from .secrets import MissingSecretError, SecretLeakError


def _load(path: str) -> Manifest:
    try:
        return load_manifest(Path(path))
    except (OSError, ManifestError) as exc:
        raise SystemExit(f"FATAL: {exc}") from None


def cmd_validate(args: argparse.Namespace) -> int:
    manifest = _load(args.manifest)
    problems = []
    for i, check in enumerate(manifest.checks):
        message = lint_check(check)
        if message:
            cid = check.get("id") if isinstance(check, dict) else None
            problems.append(f"  checks[{i}] ({cid or 'no id'}): {message}")
    print(f"manifest   : {manifest.path}")
    print(f"sha256     : {manifest.sha256}")
    print(f"cells      : {', '.join(manifest.cells)}")
    print(f"prompts    : {len(manifest.prompts)}")
    print(f"checks     : {len(manifest.checks)}" + ("" if not problems else "  (with problems)"))
    if problems:
        print("check problems (each would evaluate to the 'error' outcome at run time):")
        print("\n".join(problems))
        return 1
    print("manifest loads clean.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    manifest = _load(args.manifest)
    cells = ([c.strip() for c in args.cells.split(",") if c.strip()]
             if args.cells else list(manifest.cells))
    run_dir = Path(args.run_dir) if args.run_dir else (
        Path.cwd() / "runs" / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ"))
    config = RunConfig(
        manifest=manifest,
        run_dir=run_dir,
        cells=cells,
        groups={g.strip() for g in args.groups.split(",")} if args.groups else None,
        prompts={p.strip() for p in args.prompts.split(",")} if args.prompts else None,
        dry_run=args.dry_run,
        repeats=args.repeats,
        precondition_retries=args.precondition_retries,
        timeout_s=args.timeout,
        budget_usd=args.budget_usd,
        probe_cache_dir=Path(args.loop_probe_cache) if args.loop_probe_cache else None,
    )
    # The runner itself reports live (ruling S9: never silent -- run dir up front,
    # cell/prompt starts, turn transitions, completions); nothing to repeat here.
    try:
        result = run(config)
    except (HarnessError, MissingSecretError) as exc:
        print(f"FATAL: {exc}")
        return 2
    except SecretLeakError as exc:
        print(f"FATAL: {exc}")
        return 2
    return 1 if result.failures else 0


def cmd_interview(args: argparse.Namespace) -> int:
    try:
        questions = list(args.ask or [])
        if args.ask_file:
            extra = json.loads(Path(args.ask_file).read_text())
            if not isinstance(extra, list) or any(not isinstance(q, str) for q in extra):
                raise ValueError("--ask-file must contain a JSON array of strings")
            questions.extend(extra)
        dest = interview(Path(args.row), questions, args.budget_usd)
        return 2 if json.loads((dest / "meta.json").read_text()).get("harness_failure") else 0
    except (OSError, ValueError, KeyError, LoopError) as exc:
        print(f"REFUSED: {exc}")
        return 2


def cmd_report(args: argparse.Namespace) -> int:
    try:
        record = rebuild(Path(args.run_dir), overwrite=args.overwrite,
                         manifest_path=Path(args.manifest) if args.manifest else None)
    except (OSError, ValueError, SecretLeakError) as exc:
        print(f"FATAL: {exc}")
        return 2
    return 1 if record["failures"] else 0


def cmd_probe_driver(args: argparse.Namespace) -> int:
    driver = DRIVERS.get(args.driver)
    if driver is None:
        print(f"FATAL: unknown driver {args.driver!r} (available: {sorted(DRIVERS)})")
        return 2
    out = Path(args.out) if args.out else (
        Path.cwd() / "runs" / f"driver-probe-{args.driver}-"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}")
    try:
        record = probe_builtin_surface(driver, out, model=args.model,
                                       timeout_s=args.timeout)
    except HarnessError as exc:
        print(f"FATAL: {exc}")
        return 2
    print(f"driver     : {record['driver']['id']}  [{record['driver']['cli_version']}]")
    print(f"model      : {record['model']}")
    print(f"verdict    : {record['verdict']}")
    if record["detail"]:
        print(f"detail     : {record['detail']}")
    print(f"evidence   : {out / 'probe.json'}  (self-report -- absence of mention is not "
          "proof of absence; pair with the attribution record in the same file)")
    return 0 if record["verdict"] == PROBE_PASS else 1


def cmd_probe_egress(args: argparse.Namespace) -> int:
    driver = DRIVERS.get(args.driver)
    if driver is None:
        print(f"FATAL: unknown driver {args.driver!r} (available: {sorted(DRIVERS)})")
        return 2
    out = Path(args.out) if args.out else (
        Path.cwd() / "runs" / f"egress-probe-{args.driver}-"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}")
    try:
        record = probe_egress(driver, out, model=args.model, url=args.url,
                              timeout_s=args.timeout)
    except HarnessError as exc:
        print(f"FATAL: {exc}")
        return 2
    print(f"driver     : {record['driver']['id']}  [{record['driver']['cli_version']}]")
    print(f"model      : {record['model']}")
    print(f"url        : {record['url']}")
    print(f"verdict    : {record['verdict']}")
    if record["detail"]:
        print(f"detail     : {record['detail']}")
    print(f"evidence   : {out / 'probe.json'}  (answer verbatim, exec event stream, wire capture)")
    return 0 if record["verdict"] == EGRESS_PASS else 1


def cmd_probe_loop(args: argparse.Namespace) -> int:
    manifest = _load(args.manifest)
    cells = ([c.strip() for c in args.cells.split(",") if c.strip()]
             if args.cells else [c for c, cell in manifest.cells.items() if cell["driver"] == "loop"])
    out = Path(args.out) if args.out else (
        Path.cwd() / "runs" / f"loop-probe-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}")
    try:
        records = probe_loop_cells(manifest, cells, out, timeout_s=args.timeout,
                                   cache_dir=Path(args.loop_probe_cache) if args.loop_probe_cache else None)
    except (HarnessError, MissingSecretError) as exc:
        print(f"FATAL: {exc}")
        return 2
    print(f"evidence   : {out}  (per cell: loop-probe/probe.json and its recorder line, marked probe)")
    return 0 if all(r["verdict"] == "pass" for r in records.values()) else 1


def nonnegative_integer(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be an integer >= 0")
    return number


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer >= 1") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-e2e", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_interview = sub.add_parser("interview", help="ask unscored follow-up questions on a completed loop row")
    p_interview.add_argument("--row", required=True)
    p_interview.add_argument("--ask", action="append", help="verbatim question; repeat for sequential turns")
    p_interview.add_argument("--ask-file", help="JSON array of strings, appended after --ask questions")
    p_interview.add_argument("--budget-usd", type=float)
    p_interview.set_defaults(func=cmd_interview)

    p_report = sub.add_parser("report", help="rebuild run-level reports from retained rows")
    p_report.add_argument("--run-dir", required=True)
    p_report.add_argument("--manifest", help="pinned suite manifest, if not discoverable beside the run")
    p_report.add_argument("--overwrite", action="store_true", help="replace an existing run manifest")
    p_report.set_defaults(func=cmd_report)

    p_validate = sub.add_parser("validate", help="load the manifest and lint its checks; run nothing")
    p_validate.add_argument("--manifest", required=True)
    p_validate.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="execute the (prompt x cell) grid and record artifacts")
    p_run.add_argument("--manifest", required=True)
    p_run.add_argument("--run-dir", default=None, help="output root (default runs/<utc-timestamp>)")
    p_run.add_argument("--cells", default=None, help="comma-separated cell names (default: all)")
    p_run.add_argument("--groups", default=None, help="restrict to these prompt groups")
    p_run.add_argument("--prompts", default=None,
                       help="restrict to these prompt ids; an id named here runs in every selected "
                            "cell even if the cell's groups exclude it, marked outside_cell_groups")
    p_run.add_argument("--dry-run", action="store_true",
                       help="validate manifest, cells, and argv without calling any model or server")
    p_run.add_argument("--precondition-retries", type=nonnegative_integer, default=3,
                       help="whole-invocation replacements after unmet preconditions or "
                            "upstream unavailability (default: 3)")
    p_run.add_argument("--repeats", type=positive_integer, default=None,
                       help="repeat the selected grid N times (integer >= 1)")
    p_run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                       help=f"per-invocation timeout in seconds (default {DEFAULT_TIMEOUT_S})")
    p_run.add_argument("--budget-usd", type=float, default=None,
                       help="run-level spend cap in USD for loop cells (summed actual usage.cost); "
                            "reaching it stops the run cleanly -- completed cells stand and the stop "
                            "is recorded in run-manifest.json. Per-cell caps are the cells' budget_usd.")
    p_run.add_argument("--loop-probe-cache", default=None,
                       help="directory caching loop calibration-probe verdicts per (model, provider, "
                            "scaffold, driver version) (default: loop-probe-cache beside the run dir)")
    p_run.set_defaults(func=cmd_run)

    p_probe = sub.add_parser(
        "probe-driver",
        help="measure whether a driver's disallowed builtins reach the consumer's own tool "
             "surface (the Q2 evidence; runs one small model turn against the harness's "
             "distractor server, no manifest needed)")
    p_probe.add_argument("--driver", required=True)
    p_probe.add_argument("--model", default=None,
                         help="model for the probe turn, in the driver's vocabulary "
                              "(default: the driver's probe_model)")
    p_probe.add_argument("--out", default=None,
                         help="artifact directory (default runs/driver-probe-<driver>-<utc>)")
    p_probe.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p_probe.set_defaults(func=cmd_probe_driver)

    p_egress = sub.add_parser(
        "probe-egress",
        help="egress canary: measure under harness instruments that a driver's "
             "shell-shaped egress fails honestly (one small model turn; only the exact "
             "honest-failure protocol passes)")
    p_egress.add_argument("--driver", required=True)
    p_egress.add_argument("--model", default=None,
                          help="model for the canary turn (default: the driver's "
                               "egress_probe_model)")
    p_egress.add_argument("--url", default=EGRESS_PROBE_URL,
                          help=f"pinned fetch target (default {EGRESS_PROBE_URL})")
    p_egress.add_argument("--out", default=None,
                          help="artifact directory (default runs/egress-probe-<driver>-<utc>)")
    p_egress.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p_egress.set_defaults(func=cmd_probe_egress)

    p_loop = sub.add_parser(
        "probe-loop",
        help="run only the loop driver's calibration probe for the manifest's loop cells "
             "(one discarded request per (model, provider, scaffold, driver version), cached; "
             "no scored turn is spent) -- the S12 eligibility check for a roster")
    p_loop.add_argument("--manifest", required=True)
    p_loop.add_argument("--cells", default=None, help="comma-separated loop cell names (default: all loop cells)")
    p_loop.add_argument("--out", default=None, help="artifact directory (default runs/loop-probe-<utc>)")
    p_loop.add_argument("--loop-probe-cache", default=None,
                        help="probe verdict cache directory (default: loop-probe-cache beside --out)")
    p_loop.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    p_loop.set_defaults(func=cmd_probe_loop)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
