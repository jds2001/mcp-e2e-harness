"""The loop driver: the harness's own minimal agent loop over an OpenAI-compatible
endpoint, deployed against OpenRouter (50-drivers.md -> loop; S11, S12).

What a loop cell measures is server x model under a pinned harness scaffold, never a
shipping product; loop cells never pool with product-driver cells. The ten binding
requirements, and where each lands:

1. **Scaffold as instrument content** -- ``loop_scaffold.py``; name, version, and
   content hash travel in every artifact that names a cell.
2. **Wire capture interposes** -- the consumer (``loop_consumer.py``) is a subprocess
   that speaks HTTP to the invocation's recorder URL; the recorder forwards to the
   deployment and observes bytes, never a reconstruction.
3. **S7 per request** -- the runner compares every recorded tools array to the cell's
   surface exactly (``wire_surface_exact``); any difference BREAKS the cell.
4. **Served provider per request; strict routing on every request** -- the recorder
   extracts the response's ``provider``; ``after_turn`` compares each line to the pin
   (mismatch or absence BREAKS a pinned cell) and stamps ``reproducibility: unpinned``
   on unpinned cells. ``openrouter.provider_block`` sets ``require_parameters`` and
   ``data_collection`` on every request; a pin adds ``order``/``allow_fallbacks``/
   ``quantizations``.
5. **Knobs verbatim** -- the turn file carries the cell's knobs unchanged and the
   consumer copies them into every request body.
6. **Cost legibility** -- ``pre_run`` prints invocation count, a labeled estimate
   from the catalog's list price, and the data policy per pinned provider; the runner
   honors the run-level cap and per-cell ``budget_usd``; actual ``usage.cost`` is
   summed per invocation, cell, and run from the recorder's lines.
7. **Data policy, deny by default** -- ``data_collection: deny`` unless the cell says
   ``data_policy: allow``, which is recorded as the opt-out. Disclosure: what the
   deployment publishes about each provider (policy URLs, headquarters); stated as
   OpenRouter's assertion, never as a verified privacy property.
8. **Family separation** -- ``driver_family: loop`` and the scaffold on every mark.
9. **Eligibility by probe** -- ``cell_gate`` runs the calibration probe before the
   first scored turn of a (model, provider, scaffold, driver-version) tuple and
   caches the dated verdict; a non-pass refuses the cell before any model turn.
10. **Reasoning tokens are spend** -- the estimate names them as the unbounded term;
    the recorder's ``usage_reasoning_tokens`` is summed per invocation.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..api_capture import ApiSurfaceRecorder, loop_digest
from ..loop_scaffold import LoopScaffold, get_scaffold
from ..openrouter import (
    DEPLOYMENTS,
    OPENROUTER,
    Catalog,
    CatalogError,
    Deployment,
    estimate_invocation_usd,
    parse_pin,
    provider_block,
    served_matches_pin,
)
from .base import Driver, DriverAttributionError, TurnContext, TurnSpec

CONSUMER_MODULE = "mcp_e2e_harness.loop_consumer"
PROBE_PASS = "pass"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def loop_fields(cell: dict) -> dict:
    """The loop-specific cell fields with DR-2 null-equals-absent applied."""
    return {
        "endpoint": cell.get("endpoint"),
        "provider": cell.get("provider") or None,
        "scaffold": cell.get("scaffold"),
        "data_policy": cell.get("data_policy") or "deny",
        "budget_usd": cell.get("budget_usd"),
    }


def cell_marks(cell: dict) -> dict:
    """The identity and family marks every artifact naming a loop cell carries
    (requirement 8; S11): family, scaffold with hash, the pin or ``unpinned``,
    the reproducibility mark, the deployment, and the data policy."""
    fields = loop_fields(cell)
    scaffold = get_scaffold(fields["scaffold"] or "")
    return {
        "driver_family": "loop",
        "scaffold": {"name": fields["scaffold"],
                     "content_hash": scaffold.content_hash() if scaffold else None},
        "endpoint": fields["endpoint"],
        "provider_pin": fields["provider"] or "unpinned",
        "reproducibility": "pinned" if fields["provider"] else "unpinned",
        "data_policy": fields["data_policy"],
    }


class LoopDriver(Driver):
    id = "loop"
    executable = sys.executable
    disallowed_builtins = ()          # no builtins exist to remove; S7 is the exact check
    probe_model = ""                  # capture-capable; the calibration probe is per cell
    api_base_env = OPENROUTER.upstream_env
    api_default_upstream = OPENROUTER.upstream
    required_env = (OPENROUTER.key_env,)
    supports_sessions = True
    wire_surface_exact = True
    family = "loop"

    def __init__(self) -> None:
        # Pin -> provider display names, resolved from the endpoint listing in
        # pre_run (best effort); the consumer falls back to slug normalization.
        self._pin_names: dict[str, list[str]] = {}
        self._catalog: Catalog | None = None
        # The probe verdict per cell name, so every row's meta can say what gated it.
        self._gates: dict[str, dict] = {}

    # ------------------------------------------------------------ plumbing

    def deployment(self, cell: dict) -> Deployment:
        name = loop_fields(cell)["endpoint"]
        deployment = DEPLOYMENTS.get(name or "")
        if deployment is None:
            raise DriverAttributionError(f"unknown loop endpoint {name!r}; deployments: {sorted(DEPLOYMENTS)}")
        return deployment

    def upstream(self) -> str:
        return os.environ.get(self.api_base_env) or self.api_default_upstream

    def catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = Catalog(OPENROUTER, self.upstream())
        return self._catalog

    def cli_version(self) -> str:
        return f"loop-consumer/{__version__}"

    def write_driver_config(self, dest: Path, server_entries: dict[str, dict]) -> Path:
        path = dest / "mcp-config.json"
        path.write_text(json.dumps({"mcpServers": server_entries}, indent=2) + "\n")
        return path

    def _turn_file(self, ctx: TurnContext, *, mode: str) -> dict:
        fields = loop_fields(ctx.cell)
        deployment = self.deployment(ctx.cell)
        base = (ctx.api_base_url or "http://dry-run.invalid").rstrip("/") + deployment.api_prefix
        session_mode, session_id = ctx.session
        if mode == "probe":
            suffix, session_mode = "probe", "single"
        elif session_mode == "open":
            suffix = "crowding"
        else:
            suffix = "turn"
        pin = fields["provider"]
        return {
            "api_base": base,
            "api_key_env": deployment.key_env,
            "model": ctx.model,
            "knobs": ctx.knobs,
            "provider": provider_block(pin, fields["data_policy"]),
            "provider_pin": pin,
            "pinned_provider_names": self._pin_names.get(f"{ctx.model}|{pin}") if pin else None,
            "scaffold": fields["scaffold"],
            "mode": mode,
            "result_path": str(ctx.dest / f"loop-result-{suffix}.json"),
            "mcp_config": str(ctx.mcp_config_path) if mode == "turn" else None,
            "session_mode": session_mode,
            "session_path": str(ctx.dest / f"loop-session-{session_id}.json") if session_id else None,
            "request_timeout_s": 600.0,
            "server_order": [ctx.server_name, *ctx.extra_server_names],
        }

    def _write_turn(self, ctx: TurnContext, *, mode: str) -> TurnSpec:
        turn = self._turn_file(ctx, mode=mode)
        if mode == "probe":
            name = "loop-turn-probe.json"
        elif ctx.session[0] == "open":
            name = "loop-turn-crowding.json"
        else:
            name = "loop-turn.json"
        path = ctx.dest / name
        path.write_text(json.dumps(turn, indent=2) + "\n")
        argv = [self.executable, "-m", CONSUMER_MODULE, "--turn", str(path)]
        return TurnSpec(argv=argv, stdin_text=ctx.prompt, answer_from="stdout", env_overrides={})

    def build_turn(self, ctx: TurnContext) -> TurnSpec:
        if not ctx.cell:
            raise DriverAttributionError("the loop driver needs the cell (endpoint, scaffold, pin) to build "
                                         "a turn; the builtin-surface probe does not apply to it")
        return self._write_turn(ctx, mode="turn")

    def build_probe_turn(self, ctx: TurnContext) -> TurnSpec:
        return self._write_turn(ctx, mode="probe")

    def attribution_record(self, turn: TurnSpec) -> dict:
        """Asserted from the turn file the executed command names, never from intent."""
        argv = turn.argv

        def missing(what: str) -> DriverAttributionError:
            return DriverAttributionError(
                f"the loop turn lacks {what}. The attribution record must be asserted from the "
                "executed command; harness bug, cell refused as instrument-broken.")

        if len(argv) < 5 or argv[1:3] != ["-m", CONSUMER_MODULE] or argv[3] != "--turn":
            raise missing(f"the harness consumer module ({CONSUMER_MODULE})")
        try:
            data = json.loads(Path(argv[4]).read_text())
        except (OSError, ValueError):
            raise missing("a readable turn file") from None
        provider = data.get("provider") or {}
        if provider.get("require_parameters") is not True:
            raise missing("provider.require_parameters: true (requirement 4, hardened 2026-09-18)")
        if provider.get("data_collection") not in ("deny", "allow"):
            raise missing("provider.data_collection (deny by default; allow is the recorded opt-out)")
        pin = data.get("provider_pin")
        if pin:
            slug, quant = parse_pin(pin)
            if provider.get("order") != [slug] or provider.get("allow_fallbacks") is not False:
                raise missing(f"the pin {pin!r} as the sole allowed provider with fallbacks off")
            if quant and provider.get("quantizations") != [quant]:
                raise missing(f"quantizations: [{quant!r}] for pin {pin!r}")
        if get_scaffold(data.get("scaffold") or "") is None:
            raise missing("a registered scaffold")
        base = str(data.get("api_base") or "")
        if not (base.startswith("http://127.0.0.1:") or base.startswith("http://dry-run.invalid")):
            raise missing("an api_base pointing at the harness recorder (127.0.0.1)")
        return {
            "consumer": f"harness loop ({CONSUMER_MODULE}); no builtin tools exist to remove -- the "
                        "tools array is exactly what the harness constructs from the MCP surface",
            "recorder": base,
            "scaffold": data["scaffold"],
            "provider_policy": provider,
            "provider_pin": pin or "unpinned",
            "credential": f"{data.get('api_key_env')} by process env only; never an artifact",
        }

    # ------------------------------------------------------ runner hooks

    def cell_marks(self, cell: dict) -> dict | None:
        return cell_marks(cell)

    def cell_identity(self, cell: dict) -> list[str]:
        fields = loop_fields(cell)
        return [f"scaffold:{fields['scaffold']}", f"pin:{fields['provider'] or 'unpinned'}"]

    def pre_run(self, cells: dict[str, dict], invocations: dict[str, int],
                budget_usd: float | None, log: Callable[[str], None]) -> dict | None:
        """Cost and data-policy legibility before the first invocation (requirement 6, 7)."""
        loop_cells = {name: cell for name, cell in cells.items() if cell.get("driver") == self.id}
        if not loop_cells:
            return None
        catalog = self.catalog()
        record: dict = {"upstream": self.upstream(), "cells": {}, "estimate_usd_total": None,
                        "run_budget_usd": budget_usd, "catalog_errors": []}
        total = 0.0
        total_known = True
        count = sum(invocations.get(name, 0) for name in loop_cells)
        endpoints = ", ".join(sorted({loop_fields(c)["endpoint"] for c in loop_cells.values()}))
        log(f"loop       : {len(loop_cells)} cell(s) on {endpoints}; {count} scored invocation(s) "
            "plus one calibration probe per uncached cell")
        for name, cell in loop_cells.items():
            fields = loop_fields(cell)
            n = invocations.get(name, 0)
            entry: dict = {"model": cell["model"], "invocations": n, "provider_pin": fields["provider"] or "unpinned",
                           "data_policy": fields["data_policy"], "budget_usd": fields["budget_usd"],
                           "pricing_per_token": None, "estimate": None, "pinned_endpoint": None,
                           "provider_disclosure": None}
            try:
                pricing = catalog.model_pricing(cell["model"])
                entry["pricing_per_token"] = pricing
                estimate = estimate_invocation_usd(cell["context"], pricing)
                estimate["invocations"] = n
                estimate["usd_total"] = round(estimate["usd"] * n, 6) if estimate["usd"] is not None else None
                entry["estimate"] = estimate
                if estimate["usd_total"] is None:
                    total_known = False
                else:
                    total += estimate["usd_total"]
            except CatalogError as exc:
                record["catalog_errors"].append(f"{name}: pricing: {exc}")
                total_known = False
            pin = fields["provider"]
            if pin:
                try:
                    slug, _ = parse_pin(pin)
                    names = catalog.provider_names_for_slug(cell["model"], slug)
                    self._pin_names[f"{cell['model']}|{pin}"] = names
                    endpoint = catalog.endpoint_for_pin(cell["model"], pin)
                    entry["pinned_endpoint"] = ({
                        "tag": pin, "provider_name": endpoint.get("provider_name"),
                        "quantization": endpoint.get("quantization"), "status": endpoint.get("status"),
                        "advertises_tools": "tools" in (endpoint.get("supported_parameters") or []),
                        "pricing": endpoint.get("pricing"),
                    } if endpoint else {"tag": pin, "listed": False})
                    entry["provider_disclosure"] = self._disclosure(catalog, slug, fields["data_policy"])
                except CatalogError as exc:
                    record["catalog_errors"].append(f"{name}: endpoint listing: {exc}")
            record["cells"][name] = entry
            est = entry["estimate"]
            est_text = (f"ESTIMATE ${est['usd_total']:.4f} for {n} invocation(s) "
                        f"(~{est['estimated_input_tokens']:,} input + {est['estimated_output_tokens']:,} output "
                        f"tokens each)" if est and est["usd_total"] is not None
                        else "estimate unavailable (no list price readable)")
            log(f"loop cell  : {name}  {cell['model']}  pin={entry['provider_pin']}  {est_text}"
                + (f"  cell cap ${fields['budget_usd']:.2f}" if fields["budget_usd"] is not None else ""))
            if entry["pinned_endpoint"] is not None:
                pe = entry["pinned_endpoint"]
                if pe.get("listed") is False:
                    log(f"             pin {pin} is NOT in the endpoint listing for {cell['model']}; "
                        "the calibration probe will decide")
                else:
                    log(f"             pinned endpoint: {pe['provider_name']} quant={pe['quantization']} "
                        f"status={pe['status']} tools={'yes' if pe['advertises_tools'] else 'NO'}")
            if entry["provider_disclosure"] is not None:
                d = entry["provider_disclosure"]
                log(f"             data policy: preference data_collection={d['preference']}; "
                    f"{d['provider']}: privacy {d['privacy_policy_url'] or 'n/a'}; "
                    f"HQ {d['headquarters'] or 'n/a'} -- {d['honesty_limit']}")
        record["estimate_usd_total"] = round(total, 6) if total_known else None
        log("loop cost  : " + (f"ESTIMATE ${total:.4f} total" if total_known else "estimate incomplete")
            + " -- reasoning tokens are the unbounded term; the cap is what bounds spend"
            + (f"; run cap ${budget_usd:.2f}" if budget_usd is not None else "; no run cap set"))
        for err in record["catalog_errors"]:
            log(f"loop catalog: unavailable -- {err}")
        return record

    @staticmethod
    def _disclosure(catalog: Catalog, slug: str, data_policy: str) -> dict:
        entry = catalog.provider_record(slug) or {}
        return {
            "provider": entry.get("name") or slug,
            "slug": slug,
            "preference": data_policy,
            "privacy_policy_url": entry.get("privacy_policy_url"),
            "terms_of_service_url": entry.get("terms_of_service_url"),
            "headquarters": entry.get("headquarters"),
            "datacenters": entry.get("datacenters"),
            "honesty_limit": ("this is OpenRouter's published metadata plus a routing preference, "
                              "not a verified privacy property; backend behavior is unobservable "
                              "from here. The listing carries no machine-readable logging or "
                              "training flag (measured 2026-09-18), so the deny preference is "
                              "disclosed as sent, not checked against a per-provider mark."),
        }

    # ------------------------------------------------------ the probe gate

    def probe_key(self, cell: dict) -> tuple[dict, str]:
        """The probe's cache tuple: the spec's (model, provider tag, scaffold version,
        driver version) plus the deployment and -- a deliberate superset -- the cell's
        knobs, because the verdict measurably depends on them (a knob the pinned
        endpoint does not declare is refused at the probe; the same pair without it
        passes), so a verdict cached under one knob set must not answer for another."""
        fields = loop_fields(cell)
        scaffold = get_scaffold(fields["scaffold"] or "")
        tuple_ = {
            "endpoint": fields["endpoint"],
            "model": cell["model"],
            "provider": fields["provider"] or "unpinned",
            "scaffold": fields["scaffold"],
            "scaffold_hash": scaffold.content_hash() if scaffold else None,
            "driver_version": self.cli_version(),
            "knobs": cell["knobs"],
        }
        return tuple_, hashlib.sha256(json.dumps(tuple_, sort_keys=True).encode()).hexdigest()

    def cell_gate(self, cell_name: str, cell: dict, dest: Path, cache_dir: Path,
                  log: Callable[[str], None], timeout_s: int, scan: list[str]) -> dict:
        """Run (or recall) the calibration probe; refuse the cell on anything but a pass."""
        outcome = self._gate(cell, dest, cache_dir, log, timeout_s, scan)
        record = outcome["record"]
        self._gates[cell_name] = {k: record.get(k) for k in
                                  ("verdict", "cached", "probed_at", "served_provider", "cache_path")}
        return outcome

    def _gate(self, cell: dict, dest: Path, cache_dir: Path, log: Callable[[str], None],
              timeout_s: int, scan: list[str]) -> dict:
        tuple_, key = self.probe_key(cell)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{key}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
            except ValueError:
                cached = None
            if isinstance(cached, dict) and cached.get("tuple") == tuple_:
                verdict = cached.get("verdict")
                record = {"verdict": verdict, "cached": True, "probed_at": cached.get("probed_at"),
                          "cache_path": str(cache_path), "detail": cached.get("detail"),
                          "served_provider": cached.get("served_provider"), "tuple": tuple_,
                          "cost_usd": 0.0}
                if verdict == PROBE_PASS:
                    return {"ok": True, "reason": None, "record": record, "cost_usd": 0.0}
                return {"ok": False, "record": record, "cost_usd": 0.0,
                        "reason": (f"calibration probe verdict {verdict!r} cached on {cached.get('probed_at')} "
                                   f"for this (model, provider, scaffold, driver-version) tuple: "
                                   f"{cached.get('detail')}. The cell is instrument-broken; delete "
                                   f"{cache_path} to re-probe.")}
        record = self._run_probe(cell, dest, timeout_s, scan, log, tuple_)
        cache_path.write_text(json.dumps({"tuple": tuple_, "verdict": record["verdict"],
                                          "detail": record["detail"], "probed_at": record["probed_at"],
                                          "served_provider": record["served_provider"],
                                          "probe_dir": str(dest)}, indent=2) + "\n")
        record["cache_path"] = str(cache_path)
        if record["verdict"] == PROBE_PASS:
            return {"ok": True, "reason": None, "record": record, "cost_usd": record["cost_usd"]}
        return {"ok": False, "record": record, "cost_usd": record["cost_usd"],
                "reason": (f"calibration probe {record['verdict']!r}: {record['detail']} -- the (model, "
                           "provider) pair did not return one well-formed tool call under the cell's "
                           f"exact knobs and pin (S12); evidence in {dest / 'probe.json'}")}

    def _run_probe(self, cell: dict, dest: Path, timeout_s: int, scan: list[str],
                   log: Callable[[str], None], tuple_: dict) -> dict:
        from ..secrets import assert_clean  # local import: drivers stay importable standalone

        dest.mkdir(parents=True, exist_ok=True)
        recorder = ApiSurfaceRecorder(dest / "api-surface.jsonl", self.upstream(), mark={"probe": True})
        api_base_url = recorder.start()
        scaffold: LoopScaffold = get_scaffold(loop_fields(cell)["scaffold"])  # validated at load
        ctx = TurnContext(model=cell["model"], knobs=cell["knobs"], mcp_config_path=dest / "none",
                          server_name="", tool_surface="full", prompt=scaffold.probe_prompt,
                          dest=dest, api_base_url=api_base_url, cell=cell)
        turn = self.build_probe_turn(ctx)
        attribution = self.attribution_record(turn)
        started = _utcnow()
        exit_status, stderr_text, detail = -1, "", None
        env = dict(os.environ)
        try:
            proc = subprocess.run(turn.argv, input="", env=env, capture_output=True, text=True,
                                  timeout=timeout_s)
            exit_status, stderr_text = proc.returncode, proc.stderr
        except subprocess.TimeoutExpired:
            detail = f"probe timed out after {timeout_s}s"
        except FileNotFoundError as exc:
            detail = f"consumer not found: {exc}"
        finally:
            recorder.stop()
        result_path = Path(json.loads(Path(turn.argv[4]).read_text())["result_path"])
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        verdict = result.get("verdict") or "broken"
        if detail is None:
            detail = result.get("detail") or result.get("error")
            if verdict == "broken" and not detail:
                detail = f"consumer exited {exit_status}: {stderr_text[-400:]}"
        digest = loop_digest(dest / "api-surface.jsonl")
        record = {
            "tuple": tuple_,
            "verdict": verdict,
            "detail": detail,
            "cached": False,
            "probed_at": started,
            "finished_at": _utcnow(),
            "exit_status": exit_status,
            "served_provider": result.get("served_provider"),
            "served_providers_on_wire": digest["served_providers"],
            "tool_calls": result.get("tool_calls"),
            "tool_name": result.get("tool_name"),
            "arguments_raw": result.get("arguments_raw"),
            "schema_problems": result.get("schema_problems"),
            "breach": result.get("breach"),
            "usage": result.get("usage"),
            "cost_usd": digest["usage"]["cost_usd"],
            "wire_requests": digest["requests"],
            "command": turn.argv,
            "attribution": attribution,
            "caveat": ("Calibration evidence: one discarded request under the cell's exact knobs and "
                       "pin, offering only the scaffold's probe tool. Valid per (model, provider, "
                       "scaffold version, driver version); the cache entry is voided by any change "
                       "in that tuple. Not a scored turn; its recorder line is marked probe."),
        }
        text = json.dumps(record, indent=2) + "\n"
        assert_clean(text, scan, str(dest / "probe.json"))
        assert_clean(stderr_text, scan, str(dest / "runner-stderr.txt"))
        (dest / "probe.json").write_text(text)
        (dest / "runner-stderr.txt").write_text(stderr_text)
        return record

    # ------------------------------------------------- per-invocation digest

    def after_turn(self, turn: TurnSpec, dest: Path, cell: dict, api_surface_path: Path,
                   cell_name: str | None = None) -> dict:
        """The loop record for meta.json, and the breaches the runner voids the cell on.

        Everything wire-derived comes from the recorder's lines (S10): served
        providers per request, the usage sums, the cost. The consumer's own result
        file contributes the loop mechanics (steps, tool calls, step cap, retries)
        and the fail-fast breach it observed, which the wire lines corroborate.
        """
        fields = loop_fields(cell)
        marks = cell_marks(cell)
        digest = loop_digest(api_surface_path)
        results: dict[str, dict] = {}
        for suffix in ("turn", "crowding"):
            path = dest / f"loop-result-{suffix}.json"
            if path.exists():
                try:
                    results[suffix] = json.loads(path.read_text())
                except ValueError:
                    results[suffix] = {"error": "unreadable result file"}
        breaches: list[str] = []
        pin = fields["provider"]
        mismatches: list[dict] = []
        # A line whose response was an HTTP error (a retried 503, a refusal) served
        # no completion: it is counted as an error response, not judged against
        # the pin. A 2xx line with no provider IS judged, and fails (P-provider-pin).
        error_lines = [line for line in digest["lines"]
                       if str(line.get("provider_note") or "").startswith("upstream answered HTTP")]
        if pin:
            names = self._pin_names.get(f"{cell['model']}|{pin}")
            for line in digest["lines"]:
                if line in error_lines:
                    continue
                served = line.get("provider")
                if not served_matches_pin(served, pin, names):
                    mismatches.append({"seq": line.get("seq"), "served": served})
            if mismatches:
                breaches.append(
                    f"served-provider mismatch on a pinned cell (pin {pin!r}): "
                    + ", ".join(f"seq {m['seq']} served {m['served']!r}" for m in mismatches)
                    + " -- the environment half of the two-source rule does not hold; cell BROKEN")
        for suffix, result in results.items():
            breach = result.get("breach")
            if isinstance(breach, dict):
                breaches.append(f"loop {suffix} turn: {breach.get('kind')}: {breach.get('detail')}")
        scored = results.get("turn") or {}
        record = {
            **marks,
            "served_providers": digest["served_providers"],
            "provider_mismatches": mismatches,
            "provider_unread": digest["provider_unread"],
            "usage": digest["usage"],
            "requests_on_wire": digest["requests"],
            "error_responses": [{"seq": line.get("seq"), "note": line.get("provider_note")} for line in error_lines],
            "steps": scored.get("steps"),
            "tool_calls": scored.get("tool_calls"),
            "step_cap": scored.get("step_cap"),
            "step_cap_hit": scored.get("step_cap_hit"),
            "retries": scored.get("retries"),
            "offered_tools": scored.get("offered_tools"),
            "preturn": ({"steps": results["crowding"].get("steps"),
                         "tool_calls": results["crowding"].get("tool_calls"),
                         "step_cap_hit": results["crowding"].get("step_cap_hit")}
                        if "crowding" in results else None),
            "consumer_error": scored.get("error"),
            "probe": self._gates.get(cell_name or ""),
        }
        return {"record": record, "breaches": breaches, "cost_usd": digest["usage"]["cost_usd"]}
