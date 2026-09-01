#!/usr/bin/env python3
"""A pathological MCP server for spawn-liveness tests: initializes fine, advertises
zero tools. The S8 check must break the cell on it -- every prompt would run against
an empty surface."""
from __future__ import annotations

import json
import sys


def main() -> int:
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        if msg_id is None:
            continue
        if msg.get("method") == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "empty", "version": "0"}}
        elif msg.get("method") == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        sys.stdout.buffer.write(json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "result": result}).encode() + b"\n")
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
