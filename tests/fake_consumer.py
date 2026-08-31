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


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text())
    prompt = sys.stdin.read()

    if "do not call any tools" in prompt:
        print("I answered from memory without calling any tools.")
        return 0

    name, entry = next((n, e) for n, e in config["mcpServers"].items() if n != "shared_notes")
    env = dict(os.environ)
    env.update(entry.get("env") or {})
    with StdioMCPClient(entry["command"], entry.get("args") or [], env=env) as client:
        tools = client.list_tools()
        record = client.call_tool("list_unfiled_notes", {})
    print(f"Server {name} advertises {len(tools)} tools.")
    print(f"list_unfiled_notes returned: {json.dumps(record['response'])[:400]}")
    if "leak" in prompt.lower():
        print(f"the secret is {os.environ.get('TEST_SECRET_VAR', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
