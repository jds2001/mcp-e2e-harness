"""Crowding procedures: harness-owned, versioned, task-shaped (ruling S6)."""
from __future__ import annotations

import dataclasses

from mcp_e2e_harness.crowding import NEUTRAL_FILE_TRIAGE_V1, PROCEDURES, get_procedure


def test_registry_serves_versioned_names():
    proc = get_procedure("neutral-file-triage@1")
    assert proc is NEUTRAL_FILE_TRIAGE_V1
    assert get_procedure("neutral-file-triage@99") is None
    assert all("@" in name for name in PROCEDURES)


def test_content_hash_is_stable_and_content_sensitive():
    first = NEUTRAL_FILE_TRIAGE_V1.content_hash()
    assert first == NEUTRAL_FILE_TRIAGE_V1.content_hash()
    edited = dataclasses.replace(NEUTRAL_FILE_TRIAGE_V1, opening_prompt="different task")
    assert edited.content_hash() != first


def test_procedure_is_task_shaped_not_trivia():
    # Structural proxy for S6's requirement: there is a competing goal (an opening
    # prompt that asks for ongoing work) and registered distractor tools (folders and
    # notes for them to operate on).
    proc = NEUTRAL_FILE_TRIAGE_V1
    assert proc.opening_prompt
    assert len(proc.notes) >= 8
    assert len(proc.folders) >= 2
    assert proc.server_name
