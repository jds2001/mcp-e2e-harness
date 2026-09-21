"""WO-8's three zero-spend, zero-trace acceptance rows."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

from conftest import FakeDriver, manifest_data, write_manifest
from fake_openrouter import FakeOpenRouter
from test_driver_loop import loop_config, loop_manifest
from wo7_acceptance import REASONING

from mcp_e2e_harness.drivers.base import TurnSpec
from mcp_e2e_harness.drivers.loop import LoopDriver
from mcp_e2e_harness.manifest import load_manifest
from mcp_e2e_harness.runner import RunConfig, run


class FirstNullRouter(FakeOpenRouter):
    def answer(self, body):
        status, payload = super().answer(body)
        if not self._is_probe(body):
            payload["choices"][0] = {
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": None,
                            "reasoning_details": [{"type": "reasoning.text", "text": REASONING}]}}
        return status, payload


class RestrictedWireDriver(LoopDriver):
    def write_driver_config(self, dest, server_entries):
        # Inject a real wire mismatch: the spawn check sees the full SUT surface,
        # but this deliberately defective registration exposes only one tool.
        entries = copy.deepcopy(server_entries)
        entries["notes_sut"]["args"] += ["--allow-tools", "read_note"]
        return super().write_driver_config(dest, entries)


class EmptyProductDriver(FakeDriver):
    def build_turn(self, ctx):
        return TurnSpec([sys.executable, "-c", "pass"], stdin_text=ctx.prompt)


def experiment(root: Path, kind="loop", repeats=None):
    root.mkdir(parents=True, exist_ok=True)
    logs = []
    if kind == "product":
        data = manifest_data()
        data["cells"]["basic"]["merge_gating"] = False
        config = RunConfig(load_manifest(write_manifest(root, data)), root / "run", ["basic"],
                           drivers={"fake": EmptyProductDriver()}, log=logs.append)
        result = run(config)
    else:
        fake = FirstNullRouter()
        old = {k: os.environ.get(k) for k in ("MCP_E2E_OPENROUTER_UPSTREAM", "OPENROUTER_API_KEY")}
        os.environ["MCP_E2E_OPENROUTER_UPSTREAM"] = fake.start()
        os.environ["OPENROUTER_API_KEY"] = "sk-or-wo8-fake-0123456789"
        try:
            driver = RestrictedWireDriver() if kind == "mismatch" else LoopDriver()
            config = loop_config(root, loop_manifest(), drivers={"loop": driver}, log=logs.append, repeats=repeats)
            result = run(config)
        finally:
            fake.stop()
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    (root / "progress.txt").write_text("\n".join(logs) + "\n")
    return config, result


if __name__ == "__main__":
    experiment(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "loop")
