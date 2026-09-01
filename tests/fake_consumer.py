#!/usr/bin/env python3
"""Fake consumer for tests: a driver-shaped subprocess with no model in it.

argv[1] is the harness-written MCP config path; the prompt arrives on stdin (as it
does for real drivers). It spawns the configured server commands -- which, in a
harness run, are the trace proxy -- initializes, lists tools, and makes real tool
calls, so a test exercises the whole recording path.

Prompt markers steer behavior:
  "do not call any tools"  -> answer without any tool call (zero-trace shape)
  "leak"                   -> include $TEST_SECRET_VAR in the answer (hygiene tests)
anything else             -> call list_unfiled_notes on the first non-distractor server
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_e2e_harness.mcp_client import StdioMCPClient


def spawn_env(entry: dict) -> dict:
    """The env a fake driver hands its MCP child; FAKE_SANITIZE_VARS simulates a
    driver (codex) that strips named vars from spawned-server environments."""
    env = dict(os.environ)
    env.update(entry.get("env") or {})
    for name in os.environ.get("FAKE_SANITIZE_VARS", "").split(","):
        env.pop(name, None)
    return env


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text())
    prompt = sys.stdin.read()

    if os.environ.get("FAKE_WEB_EVENT"):
        print("fake web event: searching the live web", file=sys.stderr)

    # A real driver sends its requests to the model API; when the harness redirects
    # that traffic (FAKE_API_BASE, the capture proxy), emit one request whose body
    # carries a tools array, like the real thing would.
    base = os.environ.get("FAKE_API_BASE")
    if base and not os.environ.get("FAKE_IGNORE_API_BASE"):
        import urllib.request
        tools = [{"name": "mcp__notes_sut__list_unfiled_notes"}]
        if os.environ.get("FAKE_WIRE_BUILTIN"):
            tools.append({"name": "FakeWeb"})
        body = json.dumps({"model": "fake-model-1", "tools": tools, "messages": []}).encode()
        request = urllib.request.Request(base + "/v1/messages", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        urllib.request.urlopen(request, timeout=30).read()

    if "NO NETWORK ACCESS" in prompt:
        # The egress canary. FAKE_EGRESS_OPEN simulates a sandbox that let the
        # fetch through (or a fabricated success -- indistinguishable by design).
        if os.environ.get("FAKE_EGRESS_OPEN"):
            print("HTTP/1.1 200 OK -- fetched just fine")
        else:
            print("NO NETWORK ACCESS")
        return 0

    if "instrument check" in prompt:
        # The builtin-surface probe: enumerate tools, one name per line.
        if os.environ.get("FAKE_PROBE_SILENT"):
            print("I cannot enumerate my tools right now.")
            return 0
        name, entry = next(iter(config["mcpServers"].items()))
        with StdioMCPClient(entry["command"], entry.get("args") or [],
                            env=spawn_env(entry)) as client:
            for tool in client.list_tools():
                print(f"mcp__{name}__{tool['name']}")
        if os.environ.get("FAKE_BUILTIN_PRESENT"):
            print("FakeWeb")
        return 0

    if "do not call any tools" in prompt:
        print("I answered from memory without calling any tools.")
        return 0

    name, entry = next((n, e) for n, e in config["mcpServers"].items() if n != "shared_notes")
    with StdioMCPClient(entry["command"], entry.get("args") or [],
                        env=spawn_env(entry)) as client:
        tools = client.list_tools()
        record = client.call_tool("list_unfiled_notes", {})
    answer_lines = [f"Server {name} advertises {len(tools)} tools.",
                    f"list_unfiled_notes returned: {json.dumps(record['response'])[:400]}"]
    if "leak" in prompt.lower():
        answer_lines.append(f"the secret is {os.environ.get('TEST_SECRET_VAR', '')}")
    answer = "\n".join(answer_lines)
    # Drivers whose answer travels by file (codex -o): stdout stays an event stream.
    answer_file = os.environ.get("FAKE_ANSWER_FILE")
    if answer_file:
        Path(answer_file).write_text(answer + "\n")
        print("event: turn complete (answer written to file)")
    else:
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
