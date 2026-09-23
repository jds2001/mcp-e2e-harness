"""Cell identity canonicalization and secret-key identity."""
from __future__ import annotations

import copy

import pytest
from scenarios.loop_config import loop_manifest

from mcp_e2e_harness.drivers.loop import LoopDriver
from mcp_e2e_harness.runner import (
    cell_id_of,
    recorded_cell_env,
)


@pytest.mark.parametrize("change", [
    {"knobs": {"temperature": 1}}, {"setup": [{"tool": "x", "args": {}}]},
    {"env": {"ARM": "other"}}, {"groups": ["B"]}, {"prompts": ["A1"]},
    {"variant": "other"}, {"model": "other"}, {"context": "crowded"},
    {"tool_surface": ["one"]}, {"provider": "other"}, {"scaffold": "other"},
])
def test_identity_includes_all_components(change):
    cell = loop_manifest()["cells"]["loop-cell"]
    assert cell_id_of("a", cell, LoopDriver()) != cell_id_of("b", dict(cell, **change), LoopDriver())


def test_identity_canonical_notes_and_secrets():
    cell = loop_manifest()["cells"]["loop-cell"]
    cell.update(env={"ARM": "control", "TOKEN": {"$secret": "FIRST"}}, groups=["A", "B"])
    other = copy.deepcopy(cell)
    other.update(notes="annotation", groups=["B", "A"])
    other["env"] = {"TOKEN": {"$secret": "SECOND"}, "ARM": "control"}
    other["knobs"] = dict(reversed(list(cell["knobs"].items())))
    assert cell_id_of("a", cell) == cell_id_of("b", other)
    assert recorded_cell_env(cell) == {"ARM": "control", "TOKEN": None}
    other["env"]["OTHER_TOKEN"] = other["env"].pop("TOKEN")
    assert cell_id_of("a", cell) != cell_id_of("b", other)
