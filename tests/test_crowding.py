"""Crowding procedures: harness-owned, versioned, task-shaped (ruling S6)."""
from __future__ import annotations

import dataclasses

from mcp_e2e_harness.crowding import (
    NEUTRAL_FILE_TRIAGE_V1,
    NEUTRAL_FILE_TRIAGE_V2,
    PROCEDURES,
    get_procedure,
)

# The spec's instrument ledger pins v1 by content hash (documentation/40-instruments.md,
# 2026-08-30). This test is the implementation-side half of that pin: if the v1 object
# ever stops hashing to this value, the ledger's identity claim is broken and the
# failure must be loud here, not discovered when a run's meta disagrees with the spec.
SPEC_PINNED_V1_HASH = "d7cf153febdc9497dab62109dd666158162e11fc4596b69dfa57457e0fd400d2"


def test_registry_serves_versioned_names():
    assert get_procedure("neutral-file-triage@1") is NEUTRAL_FILE_TRIAGE_V1
    assert get_procedure("neutral-file-triage@2") is NEUTRAL_FILE_TRIAGE_V2
    assert get_procedure("neutral-file-triage@99") is None
    assert all("@" in name for name in PROCEDURES)


def test_v1_still_hashes_to_the_spec_pinned_value():
    assert NEUTRAL_FILE_TRIAGE_V1.content_hash() == SPEC_PINNED_V1_HASH


def test_content_hash_is_stable_and_content_sensitive():
    first = NEUTRAL_FILE_TRIAGE_V2.content_hash()
    assert first == NEUTRAL_FILE_TRIAGE_V2.content_hash()
    edited = dataclasses.replace(NEUTRAL_FILE_TRIAGE_V2, opening_prompt="different task")
    assert edited.content_hash() != first


def test_versions_differ_only_where_intended():
    # v2 exists because v1's pre-turn ran the task to completion (see crowding.py);
    # the fix is the opening prompt and version alone -- same notes, folders, server.
    assert NEUTRAL_FILE_TRIAGE_V1.content_hash() != NEUTRAL_FILE_TRIAGE_V2.content_hash()
    assert NEUTRAL_FILE_TRIAGE_V1.notes == NEUTRAL_FILE_TRIAGE_V2.notes
    assert NEUTRAL_FILE_TRIAGE_V1.folders == NEUTRAL_FILE_TRIAGE_V2.folders
    assert NEUTRAL_FILE_TRIAGE_V1.server_name == NEUTRAL_FILE_TRIAGE_V2.server_name
    assert NEUTRAL_FILE_TRIAGE_V1.opening_prompt != NEUTRAL_FILE_TRIAGE_V2.opening_prompt


def test_v2_preturn_is_bounded_so_the_scored_turn_lands_mid_task():
    # S6: the consumer must be GENUINELY mid-way through the task when the scored
    # prompt lands. v1 failed this live (the pre-turn filed all 12 notes); v2's
    # opening prompt bounds the pre-turn explicitly.
    assert "first four" in NEUTRAL_FILE_TRIAGE_V2.opening_prompt
    assert "stop" in NEUTRAL_FILE_TRIAGE_V2.opening_prompt


def test_procedure_is_task_shaped_not_trivia():
    # Structural proxy for S6's requirement: there is a competing goal (an opening
    # prompt that asks for ongoing work) and registered distractor tools (folders and
    # notes for them to operate on).
    proc = NEUTRAL_FILE_TRIAGE_V2
    assert proc.opening_prompt
    assert len(proc.notes) >= 8
    assert len(proc.folders) >= 2
    assert proc.server_name
