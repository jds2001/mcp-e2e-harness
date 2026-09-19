"""Row measurements -- documentation/30-checks.md, "Row measurements" (ruling S14).

A third artifact class beside checks and scoring: a mechanical value computed across
two artifacts of one row (a trace record and the row's answer), recorded beside the
row in ``meta.json`` under ``measurements.<name>``, and **never** reported as pass,
fail, vacuous, or error. A measurement is a number with a pinned method; what the
number means is the scorer's reading, so nothing here derives an outcome and nothing
here ever reaches ``checks-report.json``.

Each method is harness-owned and versioned. Its normative text is embedded verbatim
from the spec and hashed, and the row records the method name and hash beside the
values, so a number is never read without the method that produced it (the same
identity-pin treatment scaffolds and crowding procedures get in ``40-instruments.md``).

The suite declares measurements under the manifest's top-level ``measurements`` list,
reusing the checks selector (``applies_to``) and RFC 6901 pointer vocabulary.
"""
from __future__ import annotations

import hashlib
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

from . import checks

# ---------------------------------------------------------------- answer-coverage@1

# The normative method, verbatim from documentation/30-checks.md ("Method,
# normative -- the implementation embeds this numbered text verbatim and reports its
# content hash"). Edit nothing here: a content change is a new method version,
# registered beside this one, never an in-place edit.
ANSWER_COVERAGE_V1_METHOD_TEXT = "\n".join((
    "1. **Inputs:** `answer` = the row's `answer.txt`, whole; `reference` = the string the "
    "declaration's pointer resolves to; `floor` = the declared minimum span length in normalized "
    "characters (method default 64).",
    "2. **Normalization,** applied identically to both: every maximal run of Unicode whitespace "
    "becomes one ASCII space, and leading and trailing whitespace is removed. Nothing else is "
    "altered — case, punctuation, and quoting are compared as is. An offset map from each "
    "normalized character to the raw index of the character it came from is kept for the "
    "reference.",
    "3. **Matching:** find the longest common substring of the two normalized strings, ties "
    "broken by the earliest start in the answer, then the earliest start in the reference, with "
    "no junk heuristics. If its length is below `floor`, stop this branch. Otherwise record it as "
    "a span and recurse on the two remaining pairs of segments: everything before the span in "
    "both strings, and everything after it in both. Spans are therefore non-crossing: reproduced "
    "material that the answer reordered is counted at most once and only in the order the "
    "reference has it.",
    "4. **Outputs, per record:** `reference_chars_raw`, `reference_chars_normalized`, "
    "`answer_chars_raw`, `floor`, `spans` (count), `matched_chars` (sum of span lengths, "
    "normalized units), `furthest_offset` (the raw reference index one past the last character "
    "of the span that reaches furthest into the reference — reference coordinates, so a suite "
    "whose pointer already excludes its banner needs no arithmetic; 0 when there are no spans), "
    "and `share` = `matched_chars` ÷ `reference_chars_normalized`, rounded to four places; null "
    "when the reference is empty.",
))


@dataclass(frozen=True)
class Measure:
    """A registered measurement method: name@version, its verbatim text, its parameters."""
    name: str
    version: int
    method_text: str
    # Declarable method parameters: key -> (default, minimum). All integers.
    params: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.name}@{self.version}"

    def content_hash(self) -> str:
        """SHA-256 over the UTF-8 bytes of the normative method text exactly as embedded."""
        return hashlib.sha256(self.method_text.encode("utf-8")).hexdigest()


ANSWER_COVERAGE_V1 = Measure(name="answer-coverage", version=1,
                             method_text=ANSWER_COVERAGE_V1_METHOD_TEXT,
                             params={"floor": (64, 1)})

MEASURES: dict[str, Measure] = {ANSWER_COVERAGE_V1.full_name: ANSWER_COVERAGE_V1}


@dataclass(frozen=True)
class Normalized:
    """A whitespace-normalized string with its offset map back to the raw string.

    ``raw_start[i]`` is the raw index of the character normalized char ``i`` came from;
    for a collapsed whitespace run that is the run's first character, and
    ``raw_end[i]`` is one past the run's last (for any other character, ``raw_start + 1``).
    """
    text: str
    raw_start: list[int]
    raw_end: list[int]


_WS_RUN = re.compile(r"\s+")


def normalize(raw: str) -> Normalized:
    """Method step 2: each maximal Unicode-whitespace run -> one ASCII space; strip ends."""
    parts: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for m in _WS_RUN.finditer(raw):
        parts.append(raw[pos:m.start()])
        starts.extend(range(pos, m.start()))
        ends.extend(range(pos + 1, m.start() + 1))
        if m.start() > 0:  # a leading run is dropped, not collapsed
            parts.append(" ")
            starts.append(m.start())
            ends.append(m.end())
        pos = m.end()
    parts.append(raw[pos:])
    starts.extend(range(pos, len(raw)))
    ends.extend(range(pos + 1, len(raw) + 1))
    text = "".join(parts)
    if text.endswith(" "):  # the final run became a trailing space; strip it
        text = text[:-1]
        starts.pop()
        ends.pop()
    return Normalized(text, starts, ends)


def _positions_by_char(text: str) -> dict[str, list[int]]:
    table: dict[str, list[int]] = {}
    for j, ch in enumerate(text):
        table.setdefault(ch, []).append(j)
    return table


def _longest_match(a: str, b_positions: dict[str, list[int]],
                   alo: int, ahi: int, blo: int, bhi: int) -> tuple[int, int, int]:
    """Longest common substring of a[alo:ahi] and b[blo:bhi] as (i, j, k), k possibly 0.

    Ties: earliest start in ``a`` (the answer), then earliest start in ``b`` (the
    reference). Matches are enumerated by their end in ``a`` ascending, then their end
    in ``b`` ascending, and only a strictly longer match displaces the best -- for equal
    length, an earlier end is an earlier start, so the first maximum found is the
    tie-break the method states. No junk heuristics of any kind.
    """
    besti = bestj = bestk = 0
    j2len: dict[int, int] = {}
    for i in range(alo, ahi):
        newj2len: dict[int, int] = {}
        js = b_positions.get(a[i], ())
        for idx in range(bisect_left(js, blo), bisect_left(js, bhi)):
            j = js[idx]
            k = j2len.get(j - 1, 0) + 1
            newj2len[j] = k
            if k > bestk:
                besti, bestj, bestk = i - k + 1, j - k + 1, k
        j2len = newj2len
    return besti, bestj, bestk


def coverage_spans(answer_norm: str, reference_norm: str, floor: int) -> list[tuple[int, int, int]]:
    """Method step 3: the non-crossing spans as (answer_start, reference_start, length).

    Returned in reference order (which, spans being non-crossing, is answer order too).
    """
    b_positions = _positions_by_char(reference_norm)
    spans: list[tuple[int, int, int]] = []
    pending = [(0, len(answer_norm), 0, len(reference_norm))]
    while pending:
        alo, ahi, blo, bhi = pending.pop()
        if alo >= ahi or blo >= bhi:
            continue
        i, j, k = _longest_match(answer_norm, b_positions, alo, ahi, blo, bhi)
        if k < floor or k == 0:
            continue
        spans.append((i, j, k))
        pending.append((alo, i, blo, j))
        pending.append((i + k, ahi, j + k, bhi))
    return sorted(spans, key=lambda s: s[1])


@dataclass(frozen=True)
class Coverage:
    reference_chars_raw: int
    reference_chars_normalized: int
    answer_chars_raw: int
    floor: int
    spans: int
    matched_chars: int
    furthest_offset: int
    share: float | None
    # The spans themselves, (answer_start, reference_start, length) in normalized
    # units -- kept for tests and diagnosis; not part of the recorded output.
    span_details: tuple[tuple[int, int, int], ...]

    def record(self) -> dict:
        return {"reference_chars_raw": self.reference_chars_raw,
                "reference_chars_normalized": self.reference_chars_normalized,
                "answer_chars_raw": self.answer_chars_raw,
                "floor": self.floor,
                "spans": self.spans,
                "matched_chars": self.matched_chars,
                "furthest_offset": self.furthest_offset,
                "share": self.share}


def answer_coverage(answer: str, reference: str, floor: int = 64) -> Coverage:
    """``answer-coverage@1``: how much of ``reference`` the ``answer`` reproduces verbatim."""
    a = normalize(answer)
    b = normalize(reference)
    spans = coverage_spans(a.text, b.text, floor)
    matched = sum(k for _, _, k in spans)
    # One past the last raw character of the span reaching furthest into the reference.
    # When that last normalized character is a collapsed whitespace run, the span's raw
    # extent runs to the end of the run.
    furthest = max((b.raw_end[j + k - 1] for _, j, k in spans), default=0)
    share = round(matched / len(b.text), 4) if b.text else None
    return Coverage(reference_chars_raw=len(reference),
                    reference_chars_normalized=len(b.text),
                    answer_chars_raw=len(answer),
                    floor=floor, spans=len(spans), matched_chars=matched,
                    furthest_offset=furthest, share=share, span_details=tuple(spans))


# ---------------------------------------------------------------- declarations

DECLARATION_KEYS = ("name", "measure", "applies_to", "reference")


def declaration_error(entry: Any) -> str | None:
    """Structural validation of one ``measurements[]`` entry: the message, or None.

    The selector is the checks selector, linted by the checks module; the reference is
    an RFC 6901 pointer that must address one value, so the checks language's ``~each``
    extension is refused here (a measurement is against one string, never a family).
    Method parameters are validated against the measure's registry entry. Explicit
    ``null`` on an optional parameter means absent (DR-2).
    """
    if not isinstance(entry, dict):
        return "must be an object"
    measure_name = entry.get("measure")
    measure = MEASURES.get(measure_name) if isinstance(measure_name, str) else None
    allowed = set(DECLARATION_KEYS) | set(measure.params if measure else ())
    unknown = sorted(k for k in entry if not k.startswith("_") and k not in allowed)
    for key in DECLARATION_KEYS:
        if entry.get(key) is None:
            return f"missing required key {key!r}"
    if not isinstance(entry["name"], str) or not entry["name"]:
        return "'name' must be a non-empty string"
    if measure is None:
        return f"'measure' {measure_name!r} is not a harness measurement method (available: {sorted(MEASURES)})"
    if unknown:
        return (f"unknown key(s) {unknown} -- declaration keys are {list(DECLARATION_KEYS)} plus "
                f"{measure.full_name}'s parameters {sorted(measure.params)}")
    selector_problem = checks.lint_selector(entry["applies_to"])
    if selector_problem:
        return f"applies_to: {selector_problem}"
    pointer = entry["reference"]
    if not isinstance(pointer, str):
        return "'reference' must be an RFC 6901 pointer string"
    try:
        checks.pointer_tokens(pointer)
    except checks.CheckEvalError as exc:
        return f"'reference': {exc}"
    if "~each" in pointer.split("/"):
        return "'reference' must address one string; the '~each' extension is not a measurement pointer"
    for key, (_default, minimum) in measure.params.items():
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            return f"{key!r} must be an integer >= {minimum}"
    return None


def _params(measure: Measure, entry: dict) -> dict[str, int]:
    return {key: (entry[key] if entry.get(key) is not None else default)
            for key, (default, _minimum) in measure.params.items()}


def compute_measurements(declarations: list[dict], records: list[dict], answer: str) -> dict[str, list[dict]]:
    """Every declared measurement over every matched record of one row.

    Returns ``{name: [entry, ...]}`` with one entry per matched record, in trace order.
    A pointer that does not resolve to a string yields an entry whose value fields are
    all null, with a ``note`` saying why -- recorded, never an outcome.
    """
    out: dict[str, list[dict]] = {}
    for decl in declarations:
        measure = MEASURES[decl["measure"]]
        params = _params(measure, decl)
        entries: list[dict] = []
        for record in records:
            if not checks.selected(record, decl["applies_to"]):
                continue
            head = {"index": record.get("index"), "tool": record.get("tool"),
                    "method": measure.full_name, "method_hash": measure.content_hash(),
                    "reference_pointer": decl["reference"]}
            exists, values = checks.resolve_pointer(record, decl["reference"])
            if not exists or len(values) != 1:
                entries.append({**head, **_null_values(params),
                                "note": f"reference pointer {decl['reference']!r} does not resolve on this record"})
                continue
            reference = values[0]
            if not isinstance(reference, str):
                entries.append({**head, **_null_values(params),
                                "note": f"reference pointer {decl['reference']!r} resolves to "
                                        f"{type(reference).__name__}, not a string"})
                continue
            entries.append({**head, **answer_coverage(answer, reference, **params).record()})
        out[decl["name"]] = entries
    return out


def _null_values(params: dict[str, int]) -> dict:
    return {"reference_chars_raw": None, "reference_chars_normalized": None, "answer_chars_raw": None,
            **params, "spans": None, "matched_chars": None, "furthest_offset": None, "share": None}
