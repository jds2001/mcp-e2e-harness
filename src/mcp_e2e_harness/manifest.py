"""Load and validate a suite manifest -- the normative schema in documentation/20-manifest.md.

The manifest is the entire interface between a suite and the harness: one JSON file,
loaded verbatim. Keys beginning with ``_`` are commentary -- ignored here, but they
participate in the content hash, which is the SHA-256 of the file bytes (a commentary
edit is a new manifest version, which is correct: commentary carries scoring guidance).
Unknown non-underscore keys are a load error, not a warning: a typoed knob that
silently no-ops is an instrument defect waiting to be found the expensive way.

The one deliberate exception to strict key checking is the internals of ``checks``
entries: documentation/30-checks.md gives the check language its own ``error`` outcome
for unparseable rules, so a malformed check is reported per-check at evaluation time
rather than blocking the whole manifest here. ``fixtures`` and ``rubrics`` values are
opaque suite data and are likewise not key-checked.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import crowding, measurements
from .loop_scaffold import SCAFFOLDS
from .openrouter import DATA_POLICIES, DEPLOYMENTS, RESERVED_REQUEST_KEYS, parse_pin
from .secrets import is_secret_ref

SOURCING_VALUES = ("measured", "derived", "verbatim-original")
CONTEXT_VALUES = ("fresh", "crowded")
LOOP_DRIVER_ID = "loop"
LOOP_CELL_KEYS = ("endpoint", "provider", "scaffold", "data_policy", "budget_usd")


class ManifestError(ValueError):
    """A manifest that must not load; the message names the offending JSON path."""


def _fail(path: str, message: str) -> None:
    raise ManifestError(f"{path}: {message}")


def _obj(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, f"must be an object, got {type(value).__name__}")
    return value


def _given(obj: dict, key: str) -> bool:
    """Whether an OPTIONAL field is actually provided.

    DR-2 (documentation/90-open-questions.md, 20-manifest.md nullability): for every
    optional field an explicit ``null`` is equivalent to omitting it -- the worked
    example carries ``"watch": null`` throughout, and the first real suite's manifest
    was rejected for it. The one place null carries meaning of its own is
    ``pass``/``fail`` (required-but-nullable: null there means rubric-scored), which
    never goes through this helper.
    """
    return obj.get(key) is not None


def _check_keys(obj: dict, path: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    for key in required:
        if key not in obj:
            _fail(path, f"missing required key {key!r}")
    allowed = set(required) | set(optional)
    unknown = sorted(k for k in obj if not k.startswith("_") and k not in allowed)
    if unknown:
        _fail(path, f"unknown key(s) {unknown} -- unknown non-underscore keys are a load error; "
                    "commentary keys must start with '_'")


def _string(obj: dict, key: str, path: str, *, nonempty: bool = True) -> str:
    value = obj[key]
    if not isinstance(value, str) or (nonempty and not value):
        _fail(path, f"{key!r} must be a non-empty string")
    return value


def _string_list(value: Any, path: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        _fail(path, "must be a list of non-empty strings")
    if nonempty and not value:
        _fail(path, "must be a non-empty list")
    return value


def _validate_env_map(env: Any, path: str, secret_keys: set[str]) -> None:
    env = _obj(env, path)
    for key, value in env.items():
        if is_secret_ref(value):
            var = value["$secret"]
            if not isinstance(var, str) or not var:
                _fail(f"{path}.{key}", '{"$secret": VAR} must name a non-empty env var')
        elif isinstance(value, str):
            if key in secret_keys:
                _fail(f"{path}.{key}", "literal value for a key listed in server.secret_keys -- "
                                       'use {"$secret": "VAR"}; a literal secret in the manifest is a load error')
        else:
            _fail(f"{path}.{key}", 'values must be strings or {"$secret": "VAR"} references')


def _validate_server(server: Any) -> None:
    server = _obj(server, "server")
    _check_keys(server, "server", ("name", "transport"), ("secret_keys",))
    _string(server, "name", "server")
    secret_keys = set(_string_list(server["secret_keys"], "server.secret_keys", nonempty=False)
                      if _given(server, "secret_keys") else [])
    transport = _obj(server["transport"], "server.transport")
    ttype = transport.get("type")
    if ttype == "stdio":
        _check_keys(transport, "server.transport", ("type", "command"), ("args", "env"))
        _string(transport, "command", "server.transport")
        if _given(transport, "args"):
            _string_list(transport["args"], "server.transport.args", nonempty=False)
        if _given(transport, "env"):
            _validate_env_map(transport["env"], "server.transport.env", secret_keys)
    elif ttype == "http":
        _check_keys(transport, "server.transport", ("type", "url"), ("headers",))
        _string(transport, "url", "server.transport")
        if _given(transport, "headers"):
            _validate_env_map(transport["headers"], "server.transport.headers", secret_keys)
    else:
        _fail("server.transport.type", f"must be 'stdio' or 'http', got {ttype!r}")


def _validate_setup(setup: Any, path: str) -> None:
    if not isinstance(setup, list) or not setup:
        _fail(path, "must be a non-empty list of {tool, args} actions")
    for i, action in enumerate(setup):
        action = _obj(action, f"{path}[{i}]")
        _check_keys(action, f"{path}[{i}]", ("tool", "args"))
        _string(action, "tool", f"{path}[{i}]")
        _obj(action["args"], f"{path}[{i}].args")


def _validate_loop_cell(cell: dict, path: str) -> None:
    """Loop-driver cells (20-manifest.md, "Loop-driver cells", frozen 2026-09-18).

    ``endpoint`` names a deployment (harness configuration: hosts and credentials
    never appear in a manifest); ``model`` carries no routing-shortcut suffix
    (``:free`` and kin are routing preferences, not identities -- routing is
    ``provider``); ``scaffold`` names a registered harness scaffold; ``provider`` is an
    endpoint tag verbatim; ``data_policy`` is deny (default) or allow; ``budget_usd``
    a positive number; and no knob may name a request key the harness constructs.
    """
    if not _given(cell, "endpoint"):
        _fail(path, "loop cells require 'endpoint' naming a deployment "
                    f"(available: {sorted(DEPLOYMENTS)})")
    endpoint = cell["endpoint"]
    if endpoint not in DEPLOYMENTS:
        _fail(f"{path}.endpoint", f"{endpoint!r} is not a named deployment (available: "
                                  f"{sorted(DEPLOYMENTS)}); base URL and credential are harness "
                                  "configuration, never manifest content")
    if ":" in cell["model"]:
        _fail(f"{path}.model", f"{cell['model']!r} carries a routing suffix; a suffix such as ':free' "
                               "is a routing preference, not an identity -- express routing in "
                               "'provider' and pin an endpoint tag")
    if not _given(cell, "scaffold"):
        _fail(path, f"loop cells require 'scaffold' (available: {sorted(SCAFFOLDS)})")
    scaffold = cell["scaffold"]
    if scaffold not in SCAFFOLDS:
        _fail(f"{path}.scaffold", f"{scaffold!r} is not a harness scaffold (available: "
                                  f"{sorted(SCAFFOLDS)}); the scaffold is instrument content, "
                                  "pinned by content hash, and joins cell identity")
    if _given(cell, "provider"):
        try:
            parse_pin(cell["provider"])
        except ValueError as exc:
            _fail(f"{path}.provider", str(exc))
    if _given(cell, "data_policy") and cell["data_policy"] not in DATA_POLICIES:
        _fail(f"{path}.data_policy", f"must be one of {DATA_POLICIES}, got {cell['data_policy']!r}")
    if _given(cell, "budget_usd"):
        budget = cell["budget_usd"]
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
            _fail(f"{path}.budget_usd", "must be a positive number (USD)")
    reserved = sorted(k for k in cell["knobs"] if k in RESERVED_REQUEST_KEYS)
    if reserved:
        _fail(f"{path}.knobs", f"knob(s) {reserved} name request fields the harness constructs "
                               f"({list(RESERVED_REQUEST_KEYS)}); they cannot be knobs")


def _validate_cell(name: str, cell: Any, secret_keys: set[str], server_name: str) -> None:
    path = f"cells.{name}"
    cell = _obj(cell, path)
    _string(cell, "driver", path) if "driver" in cell else _fail(path, "missing required key 'driver'")
    is_loop = cell["driver"] == LOOP_DRIVER_ID
    _check_keys(cell, path,
                ("driver", "model", "knobs", "role", "context", "tool_surface", "merge_gating", "groups"),
                ("crowding", "prompts", "variant", "setup", "env", "notes")
                + (LOOP_CELL_KEYS if is_loop else ()))
    _string(cell, "model", path)
    _obj(cell["knobs"], f"{path}.knobs")
    if is_loop:
        _validate_loop_cell(cell, path)
    _string(cell, "role", path)
    context = cell["context"]
    if context not in CONTEXT_VALUES:
        _fail(f"{path}.context", f"must be one of {CONTEXT_VALUES}, got {context!r}")
    if context == "crowded":
        if not _given(cell, "crowding"):
            _fail(path, "context 'crowded' requires a crowding block {procedure, collision_review}")
        cr = _obj(cell["crowding"], f"{path}.crowding")
        _check_keys(cr, f"{path}.crowding", ("procedure", "collision_review"))
        procedure = _string(cr, "procedure", f"{path}.crowding")
        _string(cr, "collision_review", f"{path}.crowding")
        proc = crowding.get_procedure(procedure)
        if proc is None:
            _fail(f"{path}.crowding.procedure",
                  f"{procedure!r} is not a harness-provided crowding procedure "
                  f"(available: {sorted(crowding.PROCEDURES)}). Suites select procedures by "
                  "versioned name; they never supply crowding content (ruling S6).")
        elif proc.server_name == server_name:
            _fail(f"{path}.crowding.procedure",
                  f"the procedure's distractor server name {proc.server_name!r} collides with the "
                  "server under test; the crowding surface would be indistinguishable from the SUT")
    elif _given(cell, "crowding"):
        _fail(path, "crowding is only meaningful when context is 'crowded' -- "
                    "a fresh cell that names a procedure is a config that means two things")
    surface = cell["tool_surface"]
    if surface != "full":
        _string_list(surface, f"{path}.tool_surface")
    if not isinstance(cell["merge_gating"], bool):
        _fail(f"{path}.merge_gating", "must be a boolean")
    _string_list(cell["groups"], f"{path}.groups")
    if _given(cell, "prompts"):
        _string_list(cell["prompts"], f"{path}.prompts")
    if _given(cell, "variant"):
        _string(cell, "variant", path)
    if _given(cell, "setup"):
        _validate_setup(cell["setup"], f"{path}.setup")
    if _given(cell, "env"):
        _validate_env_map(cell["env"], f"{path}.env", secret_keys)
    if _given(cell, "notes") and not isinstance(cell["notes"], str):
        _fail(f"{path}.notes", "must be a string")


def _validate_prompt(i: int, entry: Any, fixtures: dict, rubrics: dict) -> str:
    path = f"prompts[{i}]"
    entry = _obj(entry, path)
    _check_keys(entry, path,
                ("id", "group", "title", "prompt", "sourcing", "pass", "fail"),
                ("variants", "fixture", "grounding", "rubric", "watch"))
    pid = _string(entry, "id", path)
    path = f"prompts[{i}] ({pid})"
    _string(entry, "group", path)
    _string(entry, "title", path)
    _string(entry, "prompt", path)
    sourcing = entry["sourcing"]
    if sourcing not in SOURCING_VALUES:
        _fail(f"{path}.sourcing", f"must be one of {SOURCING_VALUES}, got {sourcing!r}")
    if sourcing == "measured" and not isinstance(entry.get("grounding"), str):
        _fail(f"{path}.grounding", "required for sourcing 'measured': the citation -- what was "
                                   "measured, when, and how to reproduce it")
    passes, fails = entry["pass"], entry["fail"]
    for key, value in (("pass", passes), ("fail", fails)):
        if value is not None and (not isinstance(value, str) or not value):
            _fail(f"{path}.{key}", "must be a non-empty string or null")
    if (passes is None) != (fails is None):
        _fail(path, "pass and fail must be pinned together: both criteria, or both null with a rubric")
    if passes is None and not _given(entry, "rubric"):
        _fail(path, "null pass/fail requires 'rubric' naming an entry in rubrics")
    if _given(entry, "rubric"):
        rubric = _string(entry, "rubric", path)
        if rubric not in rubrics:
            _fail(f"{path}.rubric", f"{rubric!r} is not defined in rubrics")
    if _given(entry, "variants"):
        variants = _obj(entry["variants"], f"{path}.variants")
        for vname, vtext in variants.items():
            if not isinstance(vtext, str) or not vtext:
                _fail(f"{path}.variants.{vname}", "variant text must be a non-empty string")
    if _given(entry, "fixture"):
        fixture = _string(entry, "fixture", path)
        if fixture not in fixtures:
            _fail(f"{path}.fixture", f"{fixture!r} is not defined in fixtures")
    if _given(entry, "watch") and not isinstance(entry["watch"], str):
        _fail(f"{path}.watch", "must be a string")
    return pid


def validate_manifest(data: Any) -> None:
    data = _obj(data, "(top level)")
    _check_keys(data, "(top level)", ("suite", "server", "cells", "prompts"),
                ("fixtures", "rubrics", "checks", "measurements"))

    suite = _obj(data["suite"], "suite")
    _check_keys(suite, "suite", ("name", "spec", "manifest_version"))
    for key in ("name", "spec", "manifest_version"):
        _string(suite, key, "suite")

    _validate_server(data["server"])
    server = data["server"]
    server_name = server["name"]
    secret_keys = set(server.get("secret_keys") or [])

    fixtures = _obj(data["fixtures"], "fixtures") if _given(data, "fixtures") else {}
    for fname, fixture in fixtures.items():
        fixture = _obj(fixture, f"fixtures.{fname}")
        if _given(fixture, "content_hash") and not isinstance(fixture["content_hash"], str):
            _fail(f"fixtures.{fname}.content_hash", "must be a string")

    rubrics = _obj(data["rubrics"], "rubrics") if _given(data, "rubrics") else {}

    cells = _obj(data["cells"], "cells")
    if not cells:
        _fail("cells", "must define at least one cell")
    for cname, cell in cells.items():
        _validate_cell(cname, cell, secret_keys, server_name)

    prompts = data["prompts"]
    if not isinstance(prompts, list) or not prompts:
        _fail("prompts", "must be a non-empty list")
    seen: dict[str, int] = {}
    for i, entry in enumerate(prompts):
        pid = _validate_prompt(i, entry, fixtures, rubrics)
        if pid in seen:
            _fail(f"prompts[{i}]", f"duplicate id {pid!r} (also prompts[{seen[pid]}])")
        seen[pid] = i

    checks = data["checks"] if _given(data, "checks") else []
    if not isinstance(checks, list):
        _fail("checks", "must be a list of check objects (schema in documentation/30-checks.md)")
    check_ids: set[str] = set()
    for i, check in enumerate(checks):
        if not isinstance(check, dict):
            _fail(f"checks[{i}]", "must be an object")
        cid = check.get("id")
        if isinstance(cid, str) and cid:
            if cid in check_ids:
                _fail(f"checks[{i}]", f"duplicate check id {cid!r}")
            check_ids.add(cid)
        # Everything else about a check is validated at evaluation time, where a
        # malformed rule gets the language's own 'error' outcome (30-checks.md).

    # Row measurements (30-checks.md, "Row measurements"): unlike checks, a malformed
    # declaration is a load error -- a measurement has no 'error' outcome to report
    # under, because it has no outcomes at all.
    declared = data["measurements"] if _given(data, "measurements") else []
    if not isinstance(declared, list):
        _fail("measurements", "must be a list of measurement declarations (schema in documentation/30-checks.md)")
    names: set[str] = set()
    for i, entry in enumerate(declared):
        problem = measurements.declaration_error(entry)
        if problem:
            _fail(f"measurements[{i}]", problem)
        if entry["name"] in names:
            _fail(f"measurements[{i}]", f"duplicate measurement name {entry['name']!r}")
        names.add(entry["name"])

    # Cross-references from cells into prompts.
    by_id = {p["id"]: p for p in prompts}
    groups = {p["group"] for p in prompts}
    for cname, cell in cells.items():
        for g in cell["groups"]:
            if g not in groups:
                _fail(f"cells.{cname}.groups", f"group {g!r} matches no prompt -- a cell scoped to a "
                                              "group with zero prompts is a zero-denominator claim")
        allow = cell.get("prompts")
        if allow:
            for pid in allow:
                if pid not in by_id:
                    _fail(f"cells.{cname}.prompts", f"unknown prompt id {pid!r}")
                if by_id[pid]["group"] not in cell["groups"]:
                    _fail(f"cells.{cname}.prompts",
                          f"{pid!r} is in group {by_id[pid]['group']!r}, outside this cell's groups -- "
                          "the allowlist narrows groups, it cannot reach past them")
        variant = cell.get("variant")
        if variant:
            in_scope = [p for p in prompts
                        if p["group"] in cell["groups"] and (not allow or p["id"] in allow)]
            missing = [p["id"] for p in in_scope if variant not in (p.get("variants") or {})]
            if missing:
                _fail(f"cells.{cname}.variant",
                      f"variant {variant!r} is not defined on prompt(s) {missing} in this cell's scope; "
                      "running the base prompt under a variant label would mislabel the measurement")


@dataclass(frozen=True)
class Manifest:
    path: Path
    raw_bytes: bytes
    sha256: str
    data: dict

    @property
    def server(self) -> dict:
        return self.data["server"]

    @property
    def cells(self) -> dict:
        return self.data["cells"]

    @property
    def prompts(self) -> list[dict]:
        return self.data["prompts"]

    @property
    def checks(self) -> list[dict]:
        return self.data.get("checks") or []

    @property
    def fixtures(self) -> dict:
        return self.data.get("fixtures") or {}

    @property
    def measurements(self) -> list[dict]:
        return self.data.get("measurements") or []

    def prompt_by_id(self, pid: str) -> dict | None:
        return next((p for p in self.prompts if p["id"] == pid), None)


def load_manifest(path: Path | str) -> Manifest:
    """Load the suite's manifest file verbatim: no defaults merged, no re-serialization.

    The content hash is over the raw file bytes; two runs are comparable only when
    their hashes match or the suite's decision record explains the delta.
    """
    path = Path(path)
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: not valid JSON: {exc}") from None
    validate_manifest(data)
    return Manifest(path=path, raw_bytes=raw,
                    sha256=hashlib.sha256(raw).hexdigest(), data=data)
