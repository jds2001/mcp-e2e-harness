# Layer-1 checks — the declarative rule language

Ruling S2: suites express trace-conformance checks as data in the manifest, not as code plugins. The language is deliberately small. If a server contract cannot be expressed in it, that pressure is recorded in `90-open-questions.md` and decided as a language change — never solved with ad-hoc harness code, which would put server knowledge back into the harness (`10-harness.md`, division of labor).

## The trace record — pinned minimum fields

Checks evaluate over trace records, so the harness guarantees these fields on every record regardless of driver:

| field | what |
|---|---|
| `index` | 0-based position in the trace |
| `tool` | tool name as the consumer called it |
| `args` | arguments, verbatim JSON |
| `response` | the tool result, verbatim; for MCP content arrays, the parsed structure as delivered |
| `is_error` | boolean, the MCP-level error flag |
| `started_at`, `duration_ms` | timing |
| `response_bytes` | size of the serialized response |

Drivers may record more; checks may only depend on the pinned fields plus the contents of `args`/`response`.

## Check shape

```json
{
  "id": "structured-error-envelope",
  "description": "every non-success outcome is a structured object with a machine-readable kind",
  "applies_to": {"tool": "*", "when": [{"pointer": "/response/ok", "equals": false}]},
  "assert": {"all_of": [
    {"present": ["/response/error/kind"]},
    {"enum": {"pointer": "/response/error/kind", "values": ["zero-hit", "upstream-failure", "out-of-scope", "disambiguation", "redirect"]}}
  ]}
}
```

- `applies_to.tool`: a tool name, a list, or `"*"`.
- `applies_to.when`: optional list of predicates, all of which must hold for the record to be in scope. Predicate forms: `{pointer, equals: value}`, `{pointer, exists: bool}`, `{pointer, matches: regex}`.
- Pointers are RFC 6901 JSON Pointers relative to the record. One extension: a `~each` path segment maps the remainder of the pointer over an array, and the assertion must hold for every element (`/response/hits/~each/count` asserts on every hit).
- `assert`: one assertion, or a combinator. Assertions: `present: [pointers]`, `absent: [pointers]`, `matches: {pointer, regex}`, `not_matches: {pointer, regex}`, `enum: {pointer, values}`, `forbid_pattern: {regex}` (regex over the whole serialized record — the shape of the built-in secret-hygiene scan, available to suites too). Combinators: `all_of`, `any_of`, `not`.

## Evaluation semantics — these bind

Per run, per check, the harness reports exactly one of four outcomes. The distinctions exist because collapsing them is how instruments lie:

- **pass** — the check's selector matched at least one record and the assertion held on every matched record.
- **fail** — the assertion failed on at least one matched record; the report names the offending record indices and pointers. Before/after or offending *content* is in the trace; the check report carries references, not paraphrase.
- **vacuous** — the selector matched zero records. Reported distinctly and **never folded into pass**: a check that never fires is either a dead-defensive rule or a run that never exercised the surface, and both deserve eyes (the ancestor spec's dead-defensive rule, and its non-zero-denominator convention).
- **error** — the check itself could not be evaluated: unparseable rule, invalid pointer against the schema above, bad regex. This is an instrument defect; it is never reported as fail and never as vacuous — a scan that errors must not look like one that found nothing.

A missing pointer during assertion (as opposed to selection) is a **fail** of `present`-style assertions and an **error** for assertions that need a value to test (`matches` on a missing pointer is a fail of the implied presence, reported as fail with the missing pointer named — not an error, because "the field is absent" is exactly what such a check exists to catch).

Scope is per-record only in this version. Cross-record assertions (the ancestor's "no upstream failure presented in a zero-hit shape *anywhere*" was still per-record; a genuinely cross-record contract has not yet appeared) are an open question — see `90-open-questions.md` Q1 — and will be added as a language change if a real suite produces one, not preemptively.
