"""Manifest schema validation -- documentation/20-manifest.md."""
from __future__ import annotations

import copy
import hashlib

import pytest
from conftest import manifest_data, write_manifest

from mcp_e2e_harness.manifest import ManifestError, load_manifest, validate_manifest


def valid() -> dict:
    return manifest_data()


def test_valid_manifest_loads(tmp_path):
    path = write_manifest(tmp_path, valid())
    manifest = load_manifest(path)
    assert manifest.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest.server["name"] == "notes_sut"
    assert manifest.prompt_by_id("A1")["group"] == "A"


def test_hash_is_over_file_bytes_so_commentary_edits_change_it(tmp_path):
    data = valid()
    first = load_manifest(write_manifest(tmp_path, data)).sha256
    data["_about"] = "edited commentary"
    second = load_manifest(write_manifest(tmp_path, data)).sha256
    assert first != second


def test_underscore_keys_are_ignored_everywhere(tmp_path):
    data = valid()
    data["_extra"] = {"anything": 1}
    data["cells"]["basic"]["_note_to_scorer"] = "hi"
    data["prompts"][0]["_aside"] = "hi"
    validate_manifest(data)


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d.update(bogus=1), "unknown key"),
    (lambda d: d.pop("suite"), "missing required key 'suite'"),
    (lambda d: d["suite"].pop("manifest_version"), "manifest_version"),
    (lambda d: d["cells"]["basic"].update(cache={"mode": "cold"}), "unknown key"),
    (lambda d: d["cells"]["basic"].pop("knobs"), "knobs"),
    (lambda d: d["cells"]["basic"].update(context="warm"), "context"),
    (lambda d: d["cells"]["basic"].update(merge_gating="yes"), "boolean"),
    (lambda d: d["cells"]["basic"].update(groups=[]), "non-empty"),
    (lambda d: d["cells"]["basic"].update(tool_surface=[]), "non-empty"),
    (lambda d: d["prompts"][0].update(sourcing="vibes"), "sourcing"),
    (lambda d: d["prompts"][0].pop("pass"), "missing required key 'pass'"),
    (lambda d: d["prompts"][0].pop("grounding"), "grounding"),
    (lambda d: d["server"]["transport"].update(type="carrier-pigeon"), "stdio"),
    (lambda d: d["server"]["transport"].pop("command"), "command"),
])
def test_load_errors(mutate, fragment):
    data = valid()
    mutate(data)
    with pytest.raises(ManifestError, match=fragment):
        validate_manifest(data)


def test_unknown_prompt_key_is_a_load_error():
    data = valid()
    data["prompts"][0]["single_step_variant"] = "old ancestor field"
    with pytest.raises(ManifestError, match="unknown key"):
        validate_manifest(data)


def test_pass_fail_must_be_pinned_together():
    data = valid()
    data["prompts"][0]["pass"] = None
    with pytest.raises(ManifestError, match="both"):
        validate_manifest(data)


def test_null_criteria_require_a_rubric():
    data = valid()
    data["prompts"][0]["pass"] = None
    data["prompts"][0]["fail"] = None
    with pytest.raises(ManifestError, match="rubric"):
        validate_manifest(data)
    data["rubrics"] = {"honesty": {"_": "pinned rubric"}}
    data["prompts"][0]["rubric"] = "honesty"
    validate_manifest(data)


def test_rubric_must_exist():
    data = valid()
    data["prompts"][0]["rubric"] = "ghost"
    with pytest.raises(ManifestError, match="ghost"):
        validate_manifest(data)


def test_fixture_reference_must_exist():
    data = valid()
    data["prompts"][0]["fixture"] = "doc-1"
    with pytest.raises(ManifestError, match="doc-1"):
        validate_manifest(data)
    data["fixtures"] = {"doc-1": {"content_hash": "abc", "anything": "opaque"}}
    validate_manifest(data)


def test_duplicate_prompt_ids_are_a_load_error():
    data = valid()
    data["prompts"].append(copy.deepcopy(data["prompts"][0]))
    with pytest.raises(ManifestError, match="duplicate id"):
        validate_manifest(data)


def test_literal_secret_for_named_key_is_a_load_error():
    data = valid()
    data["server"]["secret_keys"] = ["API_KEY"]
    data["server"]["transport"]["env"] = {"API_KEY": "sk-live-1234"}
    with pytest.raises(ManifestError, match="literal"):
        validate_manifest(data)
    data["server"]["transport"]["env"] = {"API_KEY": {"$secret": "API_KEY"}}
    validate_manifest(data)


def test_secret_ref_shape_is_validated():
    data = valid()
    data["server"]["transport"]["env"] = {"API_KEY": {"$secret": ""}}
    with pytest.raises(ManifestError, match="secret"):
        validate_manifest(data)


def test_cell_env_honors_secret_keys():
    data = valid()
    data["server"]["secret_keys"] = ["TOKEN"]
    data["cells"]["basic"]["env"] = {"TOKEN": "literal-value"}
    with pytest.raises(ManifestError, match="literal"):
        validate_manifest(data)


def test_crowded_requires_crowding_block():
    data = valid()
    data["cells"]["basic"]["context"] = "crowded"
    with pytest.raises(ManifestError, match="crowding"):
        validate_manifest(data)
    data["cells"]["basic"]["crowding"] = {
        "procedure": "neutral-file-triage@2",
        "collision_review": "2026-08-30: office logistics is disjoint from notes_sut",
    }
    validate_manifest(data)


def test_unknown_crowding_procedure_is_a_load_error():
    data = valid()
    data["cells"]["basic"]["context"] = "crowded"
    data["cells"]["basic"]["crowding"] = {"procedure": "my-own-content@1",
                                          "collision_review": "dated"}
    with pytest.raises(ManifestError, match="harness-provided"):
        validate_manifest(data)


def test_fresh_cell_with_crowding_block_is_a_load_error():
    data = valid()
    data["cells"]["basic"]["crowding"] = {"procedure": "neutral-file-triage@2",
                                          "collision_review": "dated"}
    with pytest.raises(ManifestError, match="crowded"):
        validate_manifest(data)


def test_crowding_server_name_collision_is_a_load_error():
    data = valid()
    data["server"]["name"] = "shared_notes"  # the procedure's distractor server name
    data["cells"]["basic"]["context"] = "crowded"
    data["cells"]["basic"]["crowding"] = {"procedure": "neutral-file-triage@2",
                                          "collision_review": "dated"}
    with pytest.raises(ManifestError, match="collides"):
        validate_manifest(data)


def test_cell_group_must_match_a_prompt():
    data = valid()
    data["cells"]["basic"]["groups"] = ["A", "Z"]
    with pytest.raises(ManifestError, match="zero-denominator"):
        validate_manifest(data)


def test_cell_prompt_allowlist_is_checked():
    data = valid()
    data["cells"]["basic"]["prompts"] = ["ghost"]
    with pytest.raises(ManifestError, match="ghost"):
        validate_manifest(data)


def test_cell_allowlist_cannot_reach_past_groups():
    data = valid()
    data["prompts"].append({
        "id": "B1", "group": "B", "title": "t", "prompt": "p", "sourcing": "derived",
        "pass": "p", "fail": "f",
    })
    data["cells"]["basic"]["prompts"] = ["B1"]
    with pytest.raises(ManifestError, match="outside this cell's groups"):
        validate_manifest(data)


def test_cell_variant_must_exist_on_every_prompt_in_scope():
    data = valid()
    data["cells"]["basic"]["variant"] = "single_step"
    with pytest.raises(ManifestError, match="single_step"):
        validate_manifest(data)
    data["prompts"][0]["variants"] = {"single_step": "Read note n01 and report its title."}
    validate_manifest(data)


def test_checks_must_be_a_list_with_unique_ids():
    data = valid()
    data["checks"] = {"not": "a list"}
    with pytest.raises(ManifestError, match="list"):
        validate_manifest(data)
    data["checks"] = [{"id": "x", "applies_to": {"tool": "*"}, "assert": {"present": ["/tool"]}},
                      {"id": "x", "applies_to": {"tool": "*"}, "assert": {"present": ["/tool"]}}]
    with pytest.raises(ManifestError, match="duplicate check id"):
        validate_manifest(data)


def test_malformed_check_internals_load_anyway():
    # 30-checks.md gives the language its own 'error' outcome for unparseable rules;
    # they are reported per-check at evaluation time, not blocked at load.
    data = valid()
    data["checks"] = [{"id": "broken", "applies_to": {"tool": "*"},
                       "assert": {"no_such_assertion": True}}]
    validate_manifest(data)


def test_not_valid_json_names_the_file(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{nope")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(path)
