"""WO-7's zero-spend repeated null-final-content experiment."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fake_openrouter import FakeOpenRouter
from test_driver_loop import loop_config, loop_manifest

from mcp_e2e_harness.runner import run

REASONING = 'WO7 reasoning-only marker: next operation {"note_id":"n03","folder":"inbox"}'


class NullFinalRouter(FakeOpenRouter):
    def __init__(self):
        super().__init__()
        self.scored_finals = 0

    def answer(self, body):
        status, payload = super().answer(body)
        messages = body.get("messages") or []
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if (not self._is_probe(body) and messages[-1]["role"] == "tool"
                and last_user == "Please list the unfiled notes."):
            self.scored_finals += 1
            if self.scored_finals == 2:
                payload["choices"][0]["message"] = {
                    "role": "assistant", "content": None,
                    "reasoning_details": [{"type": "reasoning.text", "text": REASONING}]}
                payload["choices"][0]["finish_reason"] = "stop"
        return status, payload


def experiment(root: Path, crowded=False):
    root.mkdir(parents=True, exist_ok=True)
    fake = NullFinalRouter()
    old = {k: os.environ.get(k) for k in ("MCP_E2E_OPENROUTER_UPSTREAM", "OPENROUTER_API_KEY")}
    os.environ["MCP_E2E_OPENROUTER_UPSTREAM"] = fake.start()
    os.environ["OPENROUTER_API_KEY"] = "sk-or-wo7-fake-0123456789"
    logs = []
    try:
        data = loop_manifest()
        if crowded:
            data["cells"]["loop-cell"].update(context="crowded", crowding={
                "procedure": "neutral-file-triage@2", "collision_review": "fixture-only disjoint server names"})
        config = loop_config(root, data, repeats=3, log=logs.append)
        result = run(config)
        (root / "progress.txt").write_text("\n".join(logs) + "\n")
        return config, result
    finally:
        fake.stop()
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    experiment(Path(sys.argv[1]), crowded="--crowded" in sys.argv)
