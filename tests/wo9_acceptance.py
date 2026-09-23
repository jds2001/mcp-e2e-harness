"""WO-9 pre-turn failures and whole-invocation replacements, with no live spend."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from conftest import FakeDriver, manifest_data, write_manifest
from fake_openrouter import FakeOpenRouter
from test_driver_loop import loop_config, loop_manifest

from mcp_e2e_harness.drivers.base import TurnSpec
from mcp_e2e_harness.manifest import load_manifest
from mcp_e2e_harness.runner import RunConfig, run

SCORED_PROMPT = "Please list the unfiled notes."


class PreturnRouter(FakeOpenRouter):
    def __init__(self, failures=(1,), crash=False, scored_null=False):
        super().__init__()
        self.failed_attempts = set(failures)
        self.preturn_attempt = 0
        self.crash = crash
        self.scored_null = scored_null

    def answer(self, body):
        status, payload = super().answer(body)
        if self._is_probe(body):
            return status, payload
        messages = body["messages"]
        user_index = max(i for i, m in enumerate(messages) if m["role"] == "user")
        if messages[user_index]["content"] == SCORED_PROMPT:
            if self.scored_null and messages[-1]["role"] == "tool":
                payload["choices"][0]["message"] = {"role": "assistant", "content": None}
            return status, payload
        if messages[-1]["role"] == "user":
            self.preturn_attempt += 1
        calls = sum(m["role"] == "tool" for m in messages[user_index + 1:])
        if calls == 1 and self.preturn_attempt in self.failed_attempts:
            if self.crash:
                return 400, {"error": {"code": 400, "message": "fixture pre-turn failure without consumer outcome"}}
            message = {"role": "assistant", "content": None,
                       "reasoning_details": [{"text": 'Next note. {"note_id":"n02"}'}]}
        elif calls < 4:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"file_{calls}", "type": "function", "function": {
                    "name": "mcp__shared_notes__file_note",
                    "arguments": json.dumps({"note_id": f"n{calls + 1:02d}", "folder": "logistics"})}}]}
        else:
            message = {"role": "assistant", "content": "Filed four notes."}
        payload["choices"][0] = {"index": 0, "message": message,
                                   "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}
        return status, payload


class ProductPreturnDriver(FakeDriver):
    def __init__(self, crash):
        self.crash = crash

    def build_turn(self, ctx):
        if ctx.session[0] == "open":
            return TurnSpec([sys.executable, "-c", "raise SystemExit(2)" if self.crash else "pass"],
                            stdin_text=ctx.prompt)
        return super().build_turn(ctx)


def experiment(root: Path, failures=(1,), retries=None, crash=False, scored_null=False,
               budget=None, product=False, cell_budget=None, router_factory=PreturnRouter, product_driver=None):
    root.mkdir(parents=True, exist_ok=True)
    logs = []
    data = manifest_data() if product else loop_manifest()
    cell_name = "basic" if product else "loop-cell"
    data["cells"][cell_name].update(context="crowded", crowding={
        "procedure": "neutral-file-triage@2", "collision_review": "fixture disjoint server names"})
    if cell_budget is not None:
        data["cells"][cell_name]["budget_usd"] = cell_budget
    old = {k: os.environ.get(k) for k in ("MCP_E2E_OPENROUTER_UPSTREAM", "OPENROUTER_API_KEY")}
    fake = router_factory(failures, crash, scored_null)
    os.environ["MCP_E2E_OPENROUTER_UPSTREAM"] = fake.start()
    os.environ["OPENROUTER_API_KEY"] = "sk-or-wo9-test-0123456789"
    try:
        kwargs = {"repeats": 2, "log": logs.append, "budget_usd": budget}
        if retries is not None:
            kwargs["precondition_retries"] = retries
        if product:
            data["cells"][cell_name]["merge_gating"] = False
            config = RunConfig(load_manifest(write_manifest(root, data)), root / "run", [cell_name],
                               drivers={"fake": product_driver or ProductPreturnDriver(crash)}, **kwargs)
        else:
            config = loop_config(root, data, **kwargs)
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
    cases = {
        "before": {}, "disabled": {"retries": 0},
        "all-unmet": {"failures": (1, 2), "retries": 0}, "crash": {"crash": True},
        "replaced": {"retries": 3}, "exhausted": {"failures": (1, 2, 3, 4), "retries": 3},
        "scored-null": {"failures": (), "scored_null": True, "retries": 3},
        "budget": {"failures": (1, 2, 3, 4), "budget": 0.004, "retries": 3},
        "product-empty": {"product": True}, "product-crash": {"product": True, "crash": True},
        "clean": {"failures": ()},
    }
    experiment(Path(sys.argv[1]), **cases[sys.argv[2]])
