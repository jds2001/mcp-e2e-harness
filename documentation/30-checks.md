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

**Report granularity — per cell, and a failure names its row (ruled 2026-09-21, spec session).** The four outcomes are reported per check **per cell**, each with its own matched count, beside the run-wide roll-up; and a failure reference is (cell, prompt id, repetition when the run has them — `10-harness.md` → repeats — and record index), never a bare index — `index` is a position within one row's trace, and a run holds many traces, so a bare index names nothing. The run-wide roll-up is the worst outcome over cells in the order error, fail, pass, vacuous, with vacuous only when every cell is vacuous. Why this is a ruling and not a nicety: the text above said "per run, per check", and the observed report is exactly that — `runs/2026-09-18-loop-smoke/checks-report.json` covers three cells and carries one outcome per check — so one cell that fails a check hides whether every other cell passed it. The uscode-mcp spec session reached this from the other side (2026-09-21): one of its A/B arms fails a check on every record by design, and it asked whether a check can be scoped to cells. **It cannot, and that is deliberate:** the arm's server really does violate the contract the check asserts, so the fail is a true observation, and scoping the check away would delete it. What the suite needs is for that fail to be legible as the arm's and not to mask the control arm, which the per-cell breakdown gives; the suite pins the expected per-arm outcome in its preregistration, as that session already did. Until the implementation conforms (WO-6), run such arms as separate runs (`--cells`), where a run-wide report is a per-cell report. Unobserved, stated as such: no check has yet failed in any run under `runs/`, so the shape of a failure entry has never been read from an artifact; WO-6's acceptance produces the first.

Scope is per-record only in this version. Cross-record assertions (the ancestor's "no upstream failure presented in a zero-hit shape *anywhere*" was still per-record; a genuinely cross-record contract has not yet appeared) are an open question — see `90-open-questions.md` Q1 — and will be added as a language change if a real suite produces one, not preemptively.

## Row measurements — recorded, never outcomes (ruling S14, 2026-09-18)

A third artifact class beside checks and scoring: **mechanical measurements across two artifacts of one row** (a trace record and the row's answer), computed by the harness, recorded beside the row, and never reported as pass, fail, vacuous, or error. A measurement is a number with a pinned method; what the number means is the scorer's reading. Suites declare them under the manifest's top-level `measurements` list, reusing the selector and pointer vocabulary above. This is not Q1: a row measurement is per row and asserts nothing, so it is no precedent for extending the assertion language to cross-record scope.

Declaration shape:

```json
{"name": "answer_coverage", "measure": "answer-coverage@1",
 "applies_to": {"tool": ["get_public_law", "get_us_code_section"]},
 "reference": "/response/structuredContent/text/content",
 "floor": 64}
```

- `name`: the key the row's `meta.json` records it under (`measurements.<name>`, one entry per matched record, carrying the record `index` and `tool`).
- `measure`: a harness-owned, versioned method pinned in `40-instruments.md`; the row records the method name and its content hash beside the values, so a number is never read without the method that produced it.
- `applies_to`: the checks selector (`tool`, optional `when`); the measurement is computed for every matched record.
- `reference`: an RFC 6901 pointer relative to the record, resolving to the **string** the answer is measured against. It is the suite's job to point at the payload proper — for a server whose result carries a banner or a message beside the content, point at the content field, so the suite subtracts its own banner by construction. A pointer that resolves to a non-string, or does not resolve, records a null measurement with a note; it is not an outcome of any kind. Never measure against the whole serialized record: its JSON escaping differs from what a model emits.
- Method parameters (`floor` here) are recorded with the values.

### `answer-coverage@1` — how much of a reference string the answer reproduces

Purpose, and its limit stated first: the measure records **what the answer reproduces verbatim** from a tool result — the mechanical half of a "misstates its own contents" clause. It cannot read the answer's claim, and it cannot tell paraphrase from omission; a summary answer scores near zero and that is correct. Scorers read it on prompts whose expected answer quotes, and ignore it where the expected answer is a summary. Read beside the row's `finish_reason` (`10-harness.md`, recording contract): a third of the window with a model stop is a model cut; the whole window with a length stop is a knob cut; the number alone is ambiguous between them.

Method, normative — the implementation embeds this numbered text verbatim and reports its content hash:

1. **Inputs:** `answer` = the row's `answer.txt`, whole; `reference` = the string the declaration's pointer resolves to; `floor` = the declared minimum span length in normalized characters (method default 64).
2. **Normalization,** applied identically to both: every maximal run of Unicode whitespace becomes one ASCII space, and leading and trailing whitespace is removed. Nothing else is altered — case, punctuation, and quoting are compared as is. An offset map from each normalized character to the raw index of the character it came from is kept for the reference.
3. **Matching:** find the longest common substring of the two normalized strings, ties broken by the earliest start in the answer, then the earliest start in the reference, with no junk heuristics. If its length is below `floor`, stop this branch. Otherwise record it as a span and recurse on the two remaining pairs of segments: everything before the span in both strings, and everything after it in both. Spans are therefore non-crossing: reproduced material that the answer reordered is counted at most once and only in the order the reference has it.
4. **Outputs, per record:** `reference_chars_raw`, `reference_chars_normalized`, `answer_chars_raw`, `floor`, `spans` (count), `matched_chars` (sum of span lengths, normalized units), `furthest_offset` (the raw reference index one past the last character of the span that reaches furthest into the reference — reference coordinates, so a suite whose pointer already excludes its banner needs no arithmetic; 0 when there are no spans), and `share` = `matched_chars` ÷ `reference_chars_normalized`, rounded to four places; null when the reference is empty.

Reading pinned 2026-09-19 (this note is beneath the hashed text and does not change it): a normalized space that replaced a whitespace run came from the whole run, so a span whose last normalized character is such a space ends at the run's last raw character, and `furthest_offset` is one past that. Settled by measurement in the pin record — the first vectors published for this method were off by one on exactly those spans.

Reference vectors that a conforming implementation reproduces exactly are in the method's pin record (`40-instruments.md`), computed by the spec session on real rows and corrected once (2026-09-19) after the implementation's report exposed the spec session's own instrument error.
