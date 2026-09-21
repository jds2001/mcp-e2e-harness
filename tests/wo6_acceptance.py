"""Reusable $0 fake-upstream WO-6 acceptance experiment (also used by tests)."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

from fake_openrouter import FakeOpenRouter
from test_driver_loop import loop_config, loop_manifest

from mcp_e2e_harness.runner import run


def acceptance_data():
    data = loop_manifest(setup=[{"tool": "observe", "args": {}}])
    cell = data["cells"].pop("loop-cell")
    data["cells"] = {name: dict(copy.deepcopy(cell), env={"WO6_ARM": name})
                     for name in ("control", "treatment")}
    data["server"]["transport"] = {
        "type": "stdio", "command": sys.executable,
        "args": [str(Path(__file__).with_name("fake_env_server.py"))],
        "env": {"WO6_ARM": "transport-default"}}
    data["checks"] = [{"id": "control-response", "applies_to": {"tool": "*"},
                       "assert": {"matches": {"pointer": "/response/content/0/text", "regex": "^control$"}}}]
    return data


class RepeatedAnswers(FakeOpenRouter):
    def __init__(self, answerless=False):
        super().__init__()
        self.finals = 0
        self.answerless = answerless

    def answer(self, body):
        final = not self._is_probe(body) and body["messages"][-1]["role"] == "tool"
        if final:
            self.finals += 1
            self.answer_text = "Answer A" if self.finals <= 2 or self.finals >= 5 else "Answer B"
            if self.answerless and self.finals == 3:
                # S13: context-length refusal after the tool already returned.
                with self.lock:
                    self.requests.append(body)
                return 400, {"error": {"code": 400, "message": (
                    "maximum context length is 1 tokens; requested about 500 tokens")}}
        return super().answer(body)


def experiment(root, repeats=None, answerless=False):
    root.mkdir(parents=True, exist_ok=True)
    fake = RepeatedAnswers(answerless)
    old = {k: os.environ.get(k) for k in ("MCP_E2E_OPENROUTER_UPSTREAM", "OPENROUTER_API_KEY")}
    os.environ["MCP_E2E_OPENROUTER_UPSTREAM"] = fake.start()
    os.environ["OPENROUTER_API_KEY"] = "sk-or-wo6-fake-0123456789"
    logs = []
    try:
        kwargs = {} if repeats is None else {"repeats": repeats}
        config = loop_config(root, acceptance_data(), log=logs.append, **kwargs)
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
    experiment(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else None,
               answerless="--answerless" in sys.argv)
