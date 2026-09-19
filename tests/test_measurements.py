"""Row measurements (documentation/30-checks.md, "Row measurements", ruling S14).

The reference vectors here are the spec session's table in 40-instruments.md, copied
verbatim into tests/fixtures/answer-coverage/. A vector that does not reproduce is a
method-conformance defect to report, never a number to adjust (WO-4 section 4).
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from conftest import manifest_data, write_manifest

from mcp_e2e_harness import measurements as m
from mcp_e2e_harness.manifest import ManifestError, load_manifest, validate_manifest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "answer-coverage"
VECTORS = json.loads((FIXTURES / "vectors.json").read_text(encoding="utf-8"))

# Reported to the spec session for the pin record in 40-instruments.md (WO-4
# acceptance): SHA-256 over the UTF-8 bytes of the four numbered method lines exactly
# as embedded, joined by "\n" with no trailing newline. If the embedded text ever
# drifts from this, the failure must be loud here.
REPORTED_METHOD_HASH = "211931382f74e072f2c2af8c77e678660f45efa4cd2082b8d9e25b2fd4be2d6a"

# The one discrepancy found against the table (WO-4 section 4: reported, not
# adjusted). On three vector rows the furthest span ends on a collapsed whitespace run
# ("\n\n" before "[[Page 1..." at raw 6764-6765; "\n\n    " before "(a)" at raw
# 1041-1046). The method text's "one past the last character of the span" in raw
# coordinates is one past that run: 6766 and 1047. The table says 6767 and 1048 --
# one past the run, plus one more -- consistent with a reference implementation
# whose offset map sent a collapsed space to the raw index *after* its run. Every
# other value on every row, including furthest_offset on spans that end on a
# non-whitespace character (19958), reproduces exactly.
DISCREPANT_FURTHEST_OFFSET = {  # (slug, floor) -> (table value, value this implementation records)
    ("pinned-r09", 64): (6767, 6766),
    ("pinned-r09", 40): (6767, 6766),
    ("floor-crowded", 64): (6767, 6766),
    ("unpinned-r01", 64): (1048, 1047),
    ("unpinned-r01", 40): (1048, 1047),
}

VALUE_KEYS = ("reference_chars_raw", "reference_chars_normalized", "answer_chars_raw", "floor",
              "spans", "matched_chars", "furthest_offset", "share")


def _row(vector: dict) -> tuple[str, str]:
    answer = (FIXTURES / f"{vector['slug']}.answer.txt").read_text(encoding="utf-8")
    with open(FIXTURES / vector["reference"], encoding="utf-8", newline="") as fh:
        reference = fh.read()  # byte-exact: no newline translation on the reference
    return answer, reference


def _vector_id(vector: dict) -> str:
    return f"{vector['slug']}@floor{vector['floor']}"


# ------------------------------------------------------------ the method pin

def test_method_text_hashes_to_the_reported_value():
    assert m.ANSWER_COVERAGE_V1.full_name == "answer-coverage@1"
    assert m.ANSWER_COVERAGE_V1.content_hash() == REPORTED_METHOD_HASH
    assert m.ANSWER_COVERAGE_V1.content_hash() == hashlib.sha256(
        m.ANSWER_COVERAGE_V1_METHOD_TEXT.encode("utf-8")).hexdigest()
    assert m.MEASURES["answer-coverage@1"] is m.ANSWER_COVERAGE_V1


def test_method_text_is_the_spec_text_verbatim():
    # The spec is the authority; the implementation embeds it, never paraphrases it.
    spec = (Path(__file__).resolve().parent.parent / "documentation" / "30-checks.md").read_text(encoding="utf-8")
    assert m.ANSWER_COVERAGE_V1_METHOD_TEXT in spec
    assert m.ANSWER_COVERAGE_V1_METHOD_TEXT.startswith("1. **Inputs:**")
    assert m.ANSWER_COVERAGE_V1_METHOD_TEXT.count("\n") == 3


# ------------------------------------------------------------ reference vectors

@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=_vector_id)
def test_reference_vectors_reproduce(vector):
    """Every value in the 40-instruments.md table, exactly (the floor-40 rows and the zero-span row included)."""
    answer, reference = _row(vector)
    got = m.answer_coverage(answer, reference, vector["floor"]).record()
    expected = {k: vector[k] for k in VALUE_KEYS}
    key = (vector["slug"], vector["floor"])
    if key in DISCREPANT_FURTHEST_OFFSET:
        table_value, ours = DISCREPANT_FURTHEST_OFFSET[key]
        assert expected["furthest_offset"] == table_value, "vectors.json drifted from the recorded discrepancy"
        expected["furthest_offset"] = ours
    assert got == expected


@pytest.mark.parametrize("key", sorted(DISCREPANT_FURTHEST_OFFSET), ids=lambda k: f"{k[0]}@floor{k[1]}")
@pytest.mark.xfail(strict=True, reason="furthest_offset discrepancy against the 40-instruments.md table, "
                                       "reported under WO-4 section 4; see DISCREPANT_FURTHEST_OFFSET")
def test_reference_vector_furthest_offset_as_tabled(key):
    """Strict xfail: flips to a hard failure the moment the table and the method agree, so the pin gets re-checked."""
    vector = next(v for v in VECTORS["vectors"] if (v["slug"], v["floor"]) == key)
    answer, reference = _row(vector)
    assert m.answer_coverage(answer, reference, vector["floor"]).furthest_offset == vector["furthest_offset"]


def test_discrepant_spans_end_on_a_collapsed_whitespace_run():
    # The mechanism behind the discrepancy, pinned as a fact about the rows.
    vector = next(v for v in VECTORS["vectors"] if (v["slug"], v["floor"]) == ("pinned-r09", 64))
    answer, reference = _row(vector)
    cov = m.answer_coverage(answer, reference, 64)
    norm = m.normalize(reference)
    _, j, k = max(cov.span_details, key=lambda s: s[1] + s[2])
    last = j + k - 1
    assert norm.text[last] == " " and reference[norm.raw_start[last]:norm.raw_end[last]] == "\n\n"
    assert norm.raw_end[last] == 6766 == cov.furthest_offset


def test_claude_code_summary_row_at_floor_40_admits_one_heading():
    # The table's note on the zero-span row: at floor 40 a 55-character heading matches.
    vector = next(v for v in VECTORS["vectors"] if v["slug"] == "claude-code-summary")
    answer, reference = _row(vector)
    cov = m.answer_coverage(answer, reference, 40)
    assert (cov.spans, cov.matched_chars) == (1, 55)


# ------------------------------------------------------------ method mechanics

def test_normalization_collapses_unicode_whitespace_and_strips_ends():
    n = m.normalize("  x\t\n y  ")
    assert n.text == "x y"
    assert n.raw_start == [2, 3, 6] and n.raw_end == [3, 6, 7]
    assert m.normalize("").text == "" and m.normalize("  \n ").text == ""
    assert m.normalize("A  b").text == "A  b".replace("  ", " ")  # case and punctuation untouched


def test_spans_are_non_crossing_and_reordered_material_counts_once():
    ref = "alpha beta gamma delta epsilon zeta eta theta"
    answer = "delta epsilon zeta eta theta ... alpha beta gamma"  # reordered
    cov = m.answer_coverage(answer, ref, floor=5)
    # The longest common substring is the second half ("delta ... theta", 28 chars from
    # reference index 17); the first half of the reference then lies AFTER it in the
    # answer but BEFORE it in the reference, so it cannot be counted -- non-crossing.
    assert cov.span_details == ((0, 17, 28),)
    assert cov.matched_chars == 28 and cov.furthest_offset == len(ref)
    assert cov.share == round(28 / len(ref), 4)


def test_tie_break_is_earliest_in_answer_then_earliest_in_reference():
    # Four equally long candidates: "xxxxx" occurs twice in the reference and twice in the answer.
    ref = "ab_xxxxx_cd_xxxxx_ef"
    answer = "xxxxx-and-xxxxx"
    spans = m.coverage_spans(m.normalize(answer).text, m.normalize(ref).text, floor=5)
    # First pick: answer start 0 with reference start 3 (earliest in both); recursion on
    # the right halves then pairs the second answer occurrence with the second reference one.
    assert spans == [(0, 3, 5), (10, 12, 5)]
    # With one answer occurrence the reference tie-break alone decides: earliest start.
    assert m.coverage_spans("xxxxx", ref, floor=5) == [(0, 3, 5)]


def test_floor_stops_a_branch_and_the_default_is_64():
    ref = "a" * 100
    assert m.answer_coverage("a" * 63, ref).spans == 0
    assert m.answer_coverage("a" * 64, ref).spans == 1
    assert m.answer_coverage("a" * 10, ref, floor=10).record()["floor"] == 10


def test_empty_reference_yields_null_share_and_zero_offset():
    cov = m.answer_coverage("anything", "   ", floor=1)
    assert cov.record() == {"reference_chars_raw": 3, "reference_chars_normalized": 0, "answer_chars_raw": 8,
                            "floor": 1, "spans": 0, "matched_chars": 0, "furthest_offset": 0, "share": None}


# ------------------------------------------------------------ declarations

def declaration(**overrides) -> dict:
    base = {"name": "answer_coverage", "measure": "answer-coverage@1",
            "applies_to": {"tool": ["get_public_law", "get_us_code_section"]},
            "reference": "/response/structuredContent/text/content", "floor": 64}
    base.update(overrides)
    return base


def test_valid_declaration_loads_and_is_exposed_on_the_manifest(tmp_path):
    data = manifest_data(measurements=[declaration()])
    manifest = load_manifest(write_manifest(tmp_path, data))
    assert manifest.measurements == [declaration()]
    assert load_manifest(write_manifest(tmp_path, manifest_data())).measurements == []


def test_explicit_null_is_absent_for_measurements_and_for_floor():
    validate_manifest(manifest_data(measurements=None))
    validate_manifest(manifest_data(measurements=[declaration(floor=None)]))
    assert m.declaration_error(declaration(floor=None)) is None


@pytest.mark.parametrize("bad, fragment", [
    ({"name": ""}, "'name'"),
    ({"measure": "answer-coverage@2"}, "not a harness measurement method"),
    ({"applies_to": {"tool": "*", "wen": []}}, "applies_to"),
    ({"applies_to": {"tool": 7}}, "applies_to"),
    ({"reference": "response/content"}, "'reference'"),
    ({"reference": "/response/~x"}, "'reference'"),
    ({"reference": "/response/content/~each/text"}, "~each"),
    ({"reference": 3}, "'reference'"),
    ({"floor": 0}, "'floor'"),
    ({"floor": "64"}, "'floor'"),
    ({"floor": True}, "'floor'"),
    ({"window": 3}, "unknown key(s) ['window']"),
], ids=lambda x: json.dumps(x) if isinstance(x, dict) else x)
def test_malformed_declarations_are_load_errors(bad, fragment):
    with pytest.raises(ManifestError, match="measurements\\[0\\]") as exc:
        validate_manifest(manifest_data(measurements=[declaration(**bad)]))
    assert fragment in str(exc.value)


def test_missing_required_declaration_keys_are_load_errors():
    for key in ("name", "measure", "applies_to", "reference"):
        entry = declaration()
        del entry[key]
        with pytest.raises(ManifestError, match=f"missing required key {key!r}"):
            validate_manifest(manifest_data(measurements=[entry]))


def test_duplicate_measurement_names_and_non_list_are_load_errors():
    with pytest.raises(ManifestError, match="duplicate measurement name"):
        validate_manifest(manifest_data(measurements=[declaration(), declaration()]))
    with pytest.raises(ManifestError, match="measurements: must be a list"):
        validate_manifest(manifest_data(measurements={"name": "x"}))
    with pytest.raises(ManifestError, match="measurements\\[0\\]: must be an object"):
        validate_manifest(manifest_data(measurements=["x"]))


def test_commentary_keys_are_ignored_in_declarations():
    assert m.declaration_error(declaration(_why="the contents clause")) is None


# ------------------------------------------------------------ computation per row

def _record(index: int, tool: str, response: dict) -> dict:
    return {"index": index, "tool": tool, "args": {}, "response": response, "is_error": False,
            "started_at": "t", "duration_ms": 1, "response_bytes": 1}


def test_selector_reuse_matches_tool_lists_and_when_predicates():
    text = "the quick brown fox jumps over the lazy dog " * 3
    records = [_record(0, "get_public_law", {"text": {"content": text}}),
               _record(1, "search_public_laws", {"text": {"content": text}}),
               _record(2, "get_public_law", {"text": {"content": text}, "truncated": True})]
    decl = declaration(applies_to={"tool": ["get_public_law"],
                                   "when": [{"pointer": "/response/truncated", "exists": False}]},
                       reference="/response/text/content", floor=16)
    out = m.compute_measurements([decl], records, "answer: " + text)
    assert list(out) == ["answer_coverage"]
    assert [(e["index"], e["tool"]) for e in out["answer_coverage"]] == [(0, "get_public_law")]
    entry = out["answer_coverage"][0]
    assert entry["method"] == "answer-coverage@1" and entry["method_hash"] == REPORTED_METHOD_HASH
    assert entry["reference_pointer"] == "/response/text/content"
    assert entry["spans"] == 1 and entry["share"] == 1.0 and entry["floor"] == 16
    assert "note" not in entry
    # The same selector, evaluated by the checks module, agrees record for record.
    from mcp_e2e_harness import checks
    assert [checks.selected(r, decl["applies_to"]) for r in records] == [True, False, False]


def test_non_string_pointer_records_null_values_with_a_note_never_an_outcome():
    records = [_record(0, "get_public_law", {"text": {"content": {"nested": "object"}}}),
               _record(1, "get_public_law", {"text": {}}),
               _record(2, "get_public_law", {"text": {"content": ["a", "b"]}})]
    out = m.compute_measurements([declaration(reference="/response/text/content")], records, "answer")
    entries = out["answer_coverage"]
    assert [e["index"] for e in entries] == [0, 1, 2]
    assert "resolves to dict, not a string" in entries[0]["note"]
    assert "does not resolve" in entries[1]["note"]
    assert "resolves to list, not a string" in entries[2]["note"]
    for e in entries:
        assert e["method"] == "answer-coverage@1" and e["method_hash"] == REPORTED_METHOD_HASH
        assert e["floor"] == 64
        assert all(e[k] is None for k in VALUE_KEYS if k != "floor")
        assert not any(k in e for k in ("outcome", "pass", "fail", "vacuous", "error"))


def test_no_matched_record_is_an_empty_list_not_an_outcome():
    out = m.compute_measurements([declaration()], [_record(0, "other_tool", {})], "answer")
    assert out == {"answer_coverage": []}
    assert m.compute_measurements([], [_record(0, "get_public_law", {})], "answer") == {}


def test_floor_default_applies_when_undeclared():
    text = "x" * 70
    records = [_record(0, "get_public_law", {"text": {"content": text}})]
    entry = declaration(reference="/response/text/content")
    del entry["floor"]
    out = m.compute_measurements([entry], records, text)
    assert out["answer_coverage"][0]["floor"] == 64 and out["answer_coverage"][0]["spans"] == 1
    entry = copy.deepcopy(entry)
    entry["floor"] = None
    assert m.compute_measurements([entry], records, text)["answer_coverage"][0]["floor"] == 64
