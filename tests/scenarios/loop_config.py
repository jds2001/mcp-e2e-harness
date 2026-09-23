"""Loop cell configuration and local-router fixtures shared by tests."""
import copy

from conftest import manifest_data, write_manifest
from fake_openrouter import MODEL, PIN

from mcp_e2e_harness.drivers.loop import LoopDriver
from mcp_e2e_harness.manifest import load_manifest
from mcp_e2e_harness.runner import RunConfig

TEST_KEY = "sk-or-test-0123456789abcdef"

LOOP_CELL = {
    "driver": "loop",
    "model": MODEL,
    "knobs": {"max_tokens": 200, "reasoning": {"effort": "low"}},
    "role": "floor",
    "context": "fresh",
    "tool_surface": "full",
    "merge_gating": True,
    "groups": ["A"],
    "endpoint": "openrouter",
    "scaffold": "loop-scaffold@1",
    "provider": PIN,
}


def loop_manifest(**cell_overrides) -> dict:
    data = manifest_data()
    cell = copy.deepcopy(LOOP_CELL)
    cell.update(cell_overrides)
    data["cells"] = {"loop-cell": cell}
    return data


def loop_config(tmp_path, data, **overrides) -> RunConfig:
    manifest = load_manifest(write_manifest(tmp_path, data))
    defaults = dict(manifest=manifest, run_dir=tmp_path / "run", cells=list(manifest.cells),
                    drivers={"loop": LoopDriver()}, timeout_s=120, log=lambda line: None,
                    probe_cache_dir=tmp_path / "probe-cache")
    defaults.update(overrides)
    return RunConfig(**defaults)
