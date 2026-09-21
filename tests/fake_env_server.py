"""Tiny stdio server exposing its A/B environment and process identity."""
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "env-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "observe", "description": "Read the observation.",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": os.environ["WO6_ARM"]}],
                  "structuredContent": {"pid": os.getpid()}, "isError": False}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
