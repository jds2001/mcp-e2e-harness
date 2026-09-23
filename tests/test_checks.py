"""The Layer-1 check language -- documentation/30-checks.md evaluation semantics.

The four outcomes are the contract: pass, fail, vacuous, and error are distinct, and
collapsing any pair is how instruments lie.
"""
from __future__ import annotations

import pytest

from mcp_e2e_harness.checks import evaluate_checks, lint_check, resolve_pointer


def record(**overrides) -> dict:
    base = {
        "index": 0,
        "tool": "search",
        "args": {"q": "x"},
        "response": {"ok": True, "hits": [{"count": 1}, {"count": 2}]},
        "is_error": False,
        "started_at": "2026-08-30T00:00:00+00:00",
        "duration_ms": 12.5,
        "response_bytes": 64,
    }
    base.update(overrides)
    return base


def one_check(applies_to, assertion, cid="c1") -> dict:
    return {"id": cid, "applies_to": applies_to, "assert": assertion}


def outcome(check, records) -> dict:
    return evaluate_checks([check], {"cell/A/A1": records})[0]


# ---------------------------------------------------------------- pointers

def test_pointer_resolution_and_each():
    rec = record()
    assert resolve_pointer(rec, "/tool") == (True, ["search"])
    assert resolve_pointer(rec, "/response/hits/~each/count") == (True, [1, 2])
    assert resolve_pointer(rec, "/response/hits/0/count") == (True, [1])
    assert resolve_pointer(rec, "/response/missing") == (False, [])
    exists, values = resolve_pointer(rec, "/response/hits/~each/missing")
    assert exists is False and values == []


def test_each_over_empty_array_exists_vacuously():
    rec = record(response={"hits": []})
    assert resolve_pointer(rec, "/response/hits/~each/count") == (True, [])


# ---------------------------------------------------------------- outcomes

def test_pass_when_matched_and_held():
    check = one_check({"tool": "*"}, {"present": ["/response/ok"]})
    result = outcome(check, [record()])
    assert result["outcome"] == "pass"
    assert result["matched"] == 1


def test_fail_names_record_indices_and_pointers():
    check = one_check({"tool": "search"}, {"present": ["/response/error/kind"]})
    result = outcome(check, [record(index=7)])
    assert result["outcome"] == "fail"
    assert result["failures"][0]["index"] == 7
    assert "/response/error/kind" in result["failures"][0]["details"][0]
    assert result["failures"][0]["invocation"] == "cell/A/A1"


def test_vacuous_when_selector_matches_zero_records():
    check = one_check({"tool": "never_called"}, {"present": ["/response"]})
    result = outcome(check, [record()])
    assert result["outcome"] == "vacuous"
    assert result["matched"] == 0


def test_vacuous_never_folds_into_pass_on_empty_run():
    check = one_check({"tool": "*"}, {"present": ["/response"]})
    assert outcome(check, [])["outcome"] == "vacuous"


def test_error_on_bad_regex_is_not_fail_and_not_vacuous():
    check = one_check({"tool": "*"}, {"matches": {"pointer": "/tool", "regex": "("}})
    result = outcome(check, [record()])
    assert result["outcome"] == "error"
    assert "regex" in result["error"]


def test_error_on_unknown_assertion():
    check = one_check({"tool": "*"}, {"no_such_assertion": True})
    assert outcome(check, [record()])["outcome"] == "error"


def test_error_on_invalid_pointer_syntax():
    check = one_check({"tool": "*"}, {"present": ["no-leading-slash"]})
    assert outcome(check, [record()])["outcome"] == "error"


# ---------------------------------------------------------------- selection

def test_when_predicates_gate_selection():
    check = one_check({"tool": "*", "when": [{"pointer": "/response/ok", "equals": False}]},
                      {"present": ["/response/error"]})
    assert outcome(check, [record()])["outcome"] == "vacuous"
    failing = record(response={"ok": False})
    assert outcome(check, [failing])["outcome"] == "fail"


def test_when_exists_predicate():
    check = one_check({"tool": "*", "when": [{"pointer": "/response/error", "exists": True}]},
                      {"present": ["/response/error/kind"]})
    assert outcome(check, [record()])["outcome"] == "vacuous"
    sel = record(response={"error": {"kind": "zero-hit"}})
    assert outcome(check, [sel])["outcome"] == "pass"


def test_when_matches_predicate_and_tool_list():
    check = one_check({"tool": ["search", "fetch"],
                       "when": [{"pointer": "/args/q", "matches": "^x"}]},
                      {"present": ["/response"]})
    assert outcome(check, [record()])["outcome"] == "pass"
    assert outcome(check, [record(tool="other")])["outcome"] == "vacuous"


# ---------------------------------------------------------------- assertions

def test_missing_pointer_fails_matches_with_pointer_named():
    # "The field is absent" is what such a check exists to catch: fail, not error.
    check = one_check({"tool": "*"}, {"matches": {"pointer": "/response/kind", "regex": "."}})
    result = outcome(check, [record()])
    assert result["outcome"] == "fail"
    assert "missing" in result["failures"][0]["details"][0]


def test_enum_assertion():
    ok = record(response={"error": {"kind": "zero-hit"}})
    bad = record(response={"error": {"kind": "surprise"}})
    check = one_check({"tool": "*"},
                      {"enum": {"pointer": "/response/error/kind",
                                "values": ["zero-hit", "upstream-failure"]}})
    assert outcome(check, [ok])["outcome"] == "pass"
    assert outcome(check, [bad])["outcome"] == "fail"


def test_each_assertion_must_hold_on_every_element():
    check = one_check({"tool": "*"},
                      {"matches": {"pointer": "/response/hits/~each/count", "regex": r"^\d+$"}})
    assert outcome(check, [record()])["outcome"] == "pass"
    bad = record(response={"hits": [{"count": 1}, {"count": "NaN-ish"}]})
    assert outcome(check, [bad])["outcome"] == "fail"


def test_forbid_pattern_scans_the_whole_serialized_record():
    check = one_check({"tool": "*"}, {"forbid_pattern": {"regex": "sk-live-"}})
    assert outcome(check, [record()])["outcome"] == "pass"
    leaked = record(response={"note": "key sk-live-123 leaked"})
    assert outcome(check, [leaked])["outcome"] == "fail"


def test_not_matches():
    check = one_check({"tool": "*"},
                      {"not_matches": {"pointer": "/tool", "regex": "internal"}})
    assert outcome(check, [record()])["outcome"] == "pass"
    assert outcome(check, [record(tool="internal_probe")])["outcome"] == "fail"


def test_absent_assertion():
    check = one_check({"tool": "*"}, {"absent": ["/response/debug"]})
    assert outcome(check, [record()])["outcome"] == "pass"
    assert outcome(check, [record(response={"debug": 1})])["outcome"] == "fail"


# ---------------------------------------------------------------- combinators

def test_all_of_and_any_of_and_not():
    rec = record(response={"ok": False, "error": {"kind": "zero-hit"}})
    check = one_check(
        {"tool": "*", "when": [{"pointer": "/response/ok", "equals": False}]},
        {"all_of": [
            {"present": ["/response/error/kind"]},
            {"any_of": [
                {"enum": {"pointer": "/response/error/kind", "values": ["zero-hit"]}},
                {"enum": {"pointer": "/response/error/kind", "values": ["upstream-failure"]}},
            ]},
            {"not": {"present": ["/response/hits"]}},
        ]})
    assert outcome(check, [rec])["outcome"] == "pass"
    bad = record(response={"ok": False, "error": {"kind": "surprise"}})
    assert outcome(check, [bad])["outcome"] == "fail"


# ---------------------------------------------------------------- misc

def test_multiple_invocations_aggregate_into_one_outcome():
    check = one_check({"tool": "*"}, {"present": ["/response/ok"]})
    report = evaluate_checks([check], {
        "cell/A/A1": [record()],
        "cell/A/A2": [record(response={})],
    })
    assert report[0]["outcome"] == "fail"
    assert report[0]["matched"] == 2
    assert report[0]["failures"][0]["invocation"] == "cell/A/A2"


def test_check_without_id_gets_positional_id():
    report = evaluate_checks([{"applies_to": {"tool": "*"}, "assert": {"present": ["/tool"]}}],
                             {"i": [record()]})
    assert report[0]["id"] == "check[0]"
    assert report[0]["outcome"] == "error"  # missing id is a structural defect


def test_lint_check_catches_structural_problems_without_records():
    assert lint_check({"id": "ok", "applies_to": {"tool": "*"},
                       "assert": {"present": ["/tool"]}}) is None
    assert "regex" in lint_check({"id": "bad", "applies_to": {"tool": "*"},
                                  "assert": {"matches": {"pointer": "/t", "regex": "("}}})
    assert lint_check({"id": "bad", "applies_to": {"tool": "*"},
                       "assert": {"mystery": []}}) is not None


def test_checks_pool_repetitions_and_name_rows():
    check = {"id": "ok", "applies_to": {"tool": "*"}, "assert": {"present": ["/ok"]}}
    report = evaluate_checks([check], {
        "control/A/A1/r01": [{"index": 0, "ok": True}],
        "control/A/A1/r02": [{"index": 0, "ok": True}],
        "treatment/A/A1/r01": [{"index": 7}],
    }, cells=["control", "treatment", "empty"], metadata={
        "control/A/A1/r01": {"cell": "control", "prompt_id": "A1", "repetition": 1},
        "control/A/A1/r02": {"cell": "control", "prompt_id": "A1", "repetition": 2},
        "treatment/A/A1/r01": {"cell": "treatment", "prompt_id": "A1", "repetition": 1},
    })[0]
    assert report["outcome"] == "fail" and report["matched"] == 3
    assert {k: (v["outcome"], v["matched"]) for k, v in report["cells"].items()} == {
        "control": ("pass", 2), "treatment": ("fail", 1), "empty": ("vacuous", 0)}
    assert report["failures"] == [{"invocation": "treatment/A/A1/r01", "cell": "treatment",
                                    "prompt_id": "A1", "repetition": 1, "index": 7,
                                    "details": ["/ok missing"]}]


@pytest.mark.parametrize("records, expected", [
    ({"a/A/P": [], "b/A/P": []}, "vacuous"),
    ({"a/A/P": [{"tool": "search", "ok": True}], "b/A/P": []}, "pass"),
    ({"a/A/P": [{"tool": "search"}], "b/A/P": []}, "fail"),
])
def test_check_rollup_order(records, expected):
    check = {"id": "c", "applies_to": {"tool": "*"}, "assert": {"present": ["/ok"]}}
    assert evaluate_checks([check], records)[0]["outcome"] == expected


def test_cell_errors_win_and_other_cells_are_still_evaluated():
    check = {"id": "c", "applies_to": {"tool": "search", "when": [
        {"pointer": "/tool", "matches": "("}]}, "assert": {"present": ["/ok"]}}
    report = evaluate_checks([check], {"a/A/P": [{"tool": "search"}],
                                      "b/A/P": [{"tool": "other"}]}, metadata={
        "a/A/P": {"cell": "a"}, "b/A/P": {"cell": "b"}})[0]
    assert report["outcome"] == report["cells"]["a"]["outcome"] == "error"
    assert report["cells"]["b"]["outcome"] == "vacuous"
    check["assert"] = {"bad": True}
    report = evaluate_checks([check], {}, cells=["a", "b"])[0]
    assert all(c["outcome"] == "error" for c in report["cells"].values())
