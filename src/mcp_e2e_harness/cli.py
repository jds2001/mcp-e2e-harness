"""CLI for the harness.

    mcp-e2e validate --manifest suite/manifest.json
    mcp-e2e run --manifest suite/manifest.json [--run-dir runs/X] [--cells a,b]
                [--groups A,B] [--prompts A1,B2] [--dry-run] [--timeout 900]

The harness executes and records; it never scores. Pass/fail against the pinned
criteria in each meta.json is a human/spec-session judgment, recorded beside -- never
instead of -- the raw artifacts.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .checks import lint_check
from .drivers import DRIVERS
from .manifest import Manifest, ManifestError, load_manifest
from .runner import (
    DEFAULT_TIMEOUT_S,
    PROBE_PASS,
    HarnessError,
    RunConfig,
    probe_builtin_surface,
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
        timeout_s=args.timeout,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-e2e", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

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
    p_run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                       help=f"per-invocation timeout in seconds (default {DEFAULT_TIMEOUT_S})")
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
