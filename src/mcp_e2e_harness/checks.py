"""The declarative Layer-1 check language -- documentation/30-checks.md, evaluated here.

Per run, per check, exactly one of four outcomes. The distinctions exist because
collapsing them is how instruments lie:

* ``pass``    -- the selector matched at least one record and the assertion held on all.
* ``fail``    -- the assertion failed on at least one matched record; the report names
                 the offending invocations, record indices, and pointers (references
                 into the trace, never paraphrase).
* ``vacuous`` -- the selector matched zero records. Never folded into pass: a check
                 that never fires is either a dead-defensive rule or a run that never
                 exercised the surface, and both deserve eyes.
* ``error``   -- the check itself could not be evaluated (unparseable rule, invalid
                 pointer, bad regex). An instrument defect; never reported as fail and
                 never as vacuous -- a scan that errors must not look like one that
                 found nothing.

Missing-pointer semantics (binding, from the spec): a missing pointer during selection
just deselects the record; during assertion it fails ``present``-style assertions, and
value-testing assertions (``matches``/``not_matches``/``enum``) report a fail naming
the missing pointer -- "the field is absent" is exactly what such a check exists to
catch, so it is not an error.

Pointers are RFC 6901 with one extension: a ``~each`` segment maps the remainder over
an array and the assertion must hold for every element. An empty array satisfies a
for-every assertion by convention (there is nothing to violate); the vacuous outcome
at check level is where a never-exercised surface shows up.
"""
from __future__ import annotations

import json
import re
from typing import Any

ASSERTION_KEYS = ("present", "absent", "matches", "not_matches", "enum", "forbid_pattern")
COMBINATOR_KEYS = ("all_of", "any_of", "not")

# The pinned minimum trace-record fields the harness guarantees on every record,
# whatever the driver (30-checks.md). Checks may depend only on these plus the
# contents of args/response.
PINNED_RECORD_FIELDS = ("index", "tool", "args", "response", "is_error",
                        "started_at", "duration_ms", "response_bytes")


class CheckEvalError(Exception):
    """The check itself cannot be evaluated -- the 'error' outcome, an instrument defect."""


_EACH = object()


def _tokens(pointer: Any) -> list:
    if not isinstance(pointer, str):
        raise CheckEvalError(f"pointer must be a string, got {type(pointer).__name__}")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise CheckEvalError(f"invalid JSON pointer {pointer!r}: must be empty or start with '/'")
    out: list = []
    for raw in pointer.split("/")[1:]:
        if raw == "~each":
            out.append(_EACH)
            continue
        token = ""
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "~":
                nxt = raw[i + 1] if i + 1 < len(raw) else ""
                if nxt == "0":
                    token += "~"
                elif nxt == "1":
                    token += "/"
                else:
                    raise CheckEvalError(f"invalid escape '~{nxt}' in pointer {pointer!r}")
                i += 2
            else:
                token += ch
                i += 1
        out.append(token)
    return out


def pointer_tokens(pointer: Any) -> list:
    """Parse a pointer, raising CheckEvalError on bad syntax (for validators that only need that)."""
    return _tokens(pointer)


def resolve_pointer(doc: Any, pointer: str) -> tuple[bool, list]:
    """Resolve a pointer against a record: (every expanded path exists, values found).

    Without ``~each`` the values list has zero or one element. With it, one entry per
    array element that resolves; the bool is False if any element misses the remainder
    (or the node at ``~each`` is not an array).
    """
    return _walk(doc, _tokens(pointer))


def _walk(node: Any, toks: list) -> tuple[bool, list]:
    if not toks:
        return True, [node]
    tok, rest = toks[0], toks[1:]
    if tok is _EACH:
        if not isinstance(node, list):
            return False, []
        all_exist, values = True, []
        for element in node:
            exists, vals = _walk(element, rest)
            all_exist = all_exist and exists
            values.extend(vals)
        return all_exist, values
    if isinstance(node, dict):
        if tok in node:
            return _walk(node[tok], rest)
        return False, []
    if isinstance(node, list):
        try:
            index = int(tok)
        except ValueError:
            return False, []
        if 0 <= index < len(node):
            return _walk(node[index], rest)
        return False, []
    return False, []


def _compile(regex: Any) -> re.Pattern:
    if not isinstance(regex, str):
        raise CheckEvalError(f"regex must be a string, got {type(regex).__name__}")
    try:
        return re.compile(regex)
    except re.error as exc:
        raise CheckEvalError(f"bad regex {regex!r}: {exc}") from None


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _only_keys(obj: dict, path: str, allowed: tuple[str, ...], required: tuple[str, ...]) -> None:
    if not isinstance(obj, dict):
        raise CheckEvalError(f"{path} must be an object")
    unknown = sorted(k for k in obj if not k.startswith("_") and k not in allowed)
    if unknown:
        raise CheckEvalError(f"{path}: unknown key(s) {unknown}")
    for key in required:
        if key not in obj:
            raise CheckEvalError(f"{path}: missing {key!r}")


# ---------------------------------------------------------------- selection

def _selected(record: dict, applies_to: Any) -> bool:
    _only_keys(applies_to, "applies_to", ("tool", "when"), ("tool",))
    tool_spec = applies_to["tool"]
    tool = record.get("tool")
    if tool_spec == "*":
        pass
    elif isinstance(tool_spec, str):
        if tool != tool_spec:
            return False
    elif isinstance(tool_spec, list):
        if tool not in tool_spec:
            return False
    else:
        raise CheckEvalError("applies_to.tool must be a tool name, a list, or '*'")
    return all(_predicate_holds(record, pred) for pred in applies_to.get("when", []))


def _predicate_holds(record: dict, pred: Any) -> bool:
    _only_keys(pred, "when[]", ("pointer", "equals", "exists", "matches"), ("pointer",))
    forms = [k for k in ("equals", "exists", "matches") if k in pred]
    if len(forms) != 1:
        raise CheckEvalError(f"when[] needs exactly one of equals/exists/matches, got {forms}")
    exists, values = resolve_pointer(record, pred["pointer"])
    form = forms[0]
    if form == "exists":
        if not isinstance(pred["exists"], bool):
            raise CheckEvalError("when[].exists must be a boolean")
        return exists == pred["exists"]
    if not exists:
        return False
    if form == "equals":
        return all(v == pred["equals"] for v in values)
    pattern = _compile(pred["matches"])
    return all(pattern.search(_as_text(v)) for v in values)


def selected(record: dict, applies_to: Any) -> bool:
    """Whether ``applies_to`` (the checks selector) matches ``record``.

    Shared with row measurements (30-checks.md, "Row measurements"): a measurement's
    ``applies_to`` is this selector, reused rather than reimplemented.
    """
    return _selected(record, applies_to)


def lint_selector(applies_to: Any) -> str | None:
    """Structural validation of a selector alone: the error message, or None."""
    try:
        _selected(_SAMPLE_RECORD, applies_to)
    except CheckEvalError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------- assertion

def _assert_holds(record: dict, node: Any, path: str = "assert") -> tuple[bool, list[str]]:
    """Evaluate an assertion tree: (holds, failure details naming pointers)."""
    if not isinstance(node, dict):
        raise CheckEvalError(f"{path} must be an object")
    keys = [k for k in node if not k.startswith("_")]
    if len(keys) != 1:
        raise CheckEvalError(f"{path}: exactly one assertion or combinator per node, got {sorted(keys)}")
    kind = keys[0]
    body = node[kind]

    if kind == "all_of":
        details: list[str] = []
        holds = True
        for i, sub in enumerate(_require_list(body, f"{path}.all_of")):
            ok, d = _assert_holds(record, sub, f"{path}.all_of[{i}]")
            holds = holds and ok
            details.extend(d)
        return holds, details
    if kind == "any_of":
        subs = _require_list(body, f"{path}.any_of")
        collected: list[str] = []
        for i, sub in enumerate(subs):
            ok, d = _assert_holds(record, sub, f"{path}.any_of[{i}]")
            if ok:
                return True, []
            collected.extend(d)
        return False, [f"{path}: no branch of any_of held"] + collected
    if kind == "not":
        ok, _ = _assert_holds(record, body, f"{path}.not")
        if ok:
            return False, [f"{path}: negated assertion held"]
        return True, []

    if kind == "present":
        details = []
        for ptr in _require_str_list(body, f"{path}.present"):
            exists, _ = resolve_pointer(record, ptr)
            if not exists:
                details.append(f"{ptr} missing")
        return (not details), details
    if kind == "absent":
        details = []
        for ptr in _require_str_list(body, f"{path}.absent"):
            _, values = resolve_pointer(record, ptr)
            if values:
                details.append(f"{ptr} present")
        return (not details), details
    if kind in ("matches", "not_matches"):
        _only_keys(body, f"{path}.{kind}", ("pointer", "regex"), ("pointer", "regex"))
        pattern = _compile(body["regex"])
        exists, values = resolve_pointer(record, body["pointer"])
        if not exists:
            # A fail of the implied presence, reported with the pointer named -- not an
            # error, because "the field is absent" is what such a check exists to catch.
            return False, [f"{body['pointer']} missing (implied presence)"]
        if kind == "matches":
            bad = [v for v in values if not pattern.search(_as_text(v))]
            return (not bad), [f"{body['pointer']} !~ /{body['regex']}/" for _ in bad]
        bad = [v for v in values if pattern.search(_as_text(v))]
        return (not bad), [f"{body['pointer']} =~ /{body['regex']}/ (forbidden)" for _ in bad]
    if kind == "enum":
        _only_keys(body, f"{path}.enum", ("pointer", "values"), ("pointer", "values"))
        allowed = _require_list(body["values"], f"{path}.enum.values")
        exists, values = resolve_pointer(record, body["pointer"])
        if not exists:
            return False, [f"{body['pointer']} missing (implied presence)"]
        bad = [v for v in values if v not in allowed]
        return (not bad), [f"{body['pointer']} value {json.dumps(v)} not in enum" for v in bad]
    if kind == "forbid_pattern":
        _only_keys(body, f"{path}.forbid_pattern", ("regex",), ("regex",))
        pattern = _compile(body["regex"])
        hit = pattern.search(json.dumps(record, sort_keys=True))
        return (hit is None), ([f"forbidden pattern /{body['regex']}/ found in record"] if hit else [])

    raise CheckEvalError(f"{path}: unknown assertion/combinator {kind!r} "
                         f"(assertions: {ASSERTION_KEYS}; combinators: {COMBINATOR_KEYS})")


def _require_list(value: Any, path: str) -> list:
    if not isinstance(value, list) or not value:
        raise CheckEvalError(f"{path} must be a non-empty list")
    return value


def _require_str_list(value: Any, path: str) -> list[str]:
    for v in _require_list(value, path):
        if not isinstance(v, str):
            raise CheckEvalError(f"{path} must contain only pointer strings")
    return value


# ---------------------------------------------------------------- structural lint

_SAMPLE_RECORD = {"index": 0, "tool": "", "args": {}, "response": {}, "is_error": False,
                  "started_at": "", "duration_ms": 0, "response_bytes": 0}


def lint_check(check: dict) -> str | None:
    """Structural validation without records: returns the error message, or None.

    Runs the selector and assertion machinery against a sample record so pointer
    syntax, regexes, and rule shape are exercised; outcome-irrelevant here.
    """
    try:
        if not isinstance(check, dict):
            raise CheckEvalError("check must be an object")
        _only_keys(check, "check", ("id", "description", "applies_to", "assert"),
                   ("id", "applies_to", "assert"))
        if not isinstance(check.get("id"), str) or not check["id"]:
            raise CheckEvalError("check.id must be a non-empty string")
        _selected(_SAMPLE_RECORD, check["applies_to"])
        _assert_holds(_SAMPLE_RECORD, check["assert"])
    except CheckEvalError as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------- evaluation

def evaluate_checks(checks: list[dict],
                    records_by_invocation: dict[str, list[dict]]) -> list[dict]:
    """One outcome per check over the whole run, with per-record failure references.

    ``records_by_invocation`` maps an invocation label (cell/group/prompt) to its
    parsed trace records. The report states the matched-record count -- a claim over a
    set states the set's size (non-zero denominator, 00-INDEX conventions).
    """
    report = []
    for i, check in enumerate(checks):
        cid = check.get("id") if isinstance(check, dict) else None
        cid = cid if isinstance(cid, str) and cid else f"check[{i}]"
        entry: dict[str, Any] = {"id": cid,
                                 "description": check.get("description") if isinstance(check, dict) else None}
        structural = lint_check(check) if isinstance(check, dict) else "check must be an object"
        if structural:
            entry.update(outcome="error", error=structural, matched=0, failures=[])
            report.append(entry)
            continue
        matched = 0
        failures: list[dict] = []
        error: str | None = None
        for invocation, records in records_by_invocation.items():
            for record in records:
                try:
                    if not _selected(record, check["applies_to"]):
                        continue
                    matched += 1
                    holds, details = _assert_holds(record, check["assert"])
                except CheckEvalError as exc:
                    error = str(exc)
                    break
                if not holds:
                    failures.append({"invocation": invocation,
                                     "index": record.get("index"),
                                     "details": details})
            if error:
                break
        if error:
            entry.update(outcome="error", error=error, matched=matched, failures=[])
        elif failures:
            entry.update(outcome="fail", matched=matched, failures=failures)
        elif matched:
            entry.update(outcome="pass", matched=matched, failures=[])
        else:
            entry.update(outcome="vacuous", matched=0, failures=[])
        report.append(entry)
    return report
