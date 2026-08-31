"""Minimal stdio MCP server serving a crowding procedure's distractor tools.

Registered by the harness beside the server under test in ``crowded`` cells
(ruling S6): the consumer works this mundane, domain-neutral task before -- and
around -- the scored prompt. The tool content comes entirely from the named
:mod:`mcp_e2e_harness.crowding` procedure; nothing here knows anything about any
server under test.

Filed/unfiled state persists across restarts via ``--state-file`` (the driver spawns
a fresh server process per turn; the task must stay coherent across the crowding
pre-turn and the scored turn or the "mid-task" claim is fiction).

    python -m mcp_e2e_harness.distractor --procedure neutral-file-triage@1 \
        --state-file /path/state.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .crowding import CrowdingProcedure, get_procedure
from .mcp_client import PROTOCOL_VERSION


def _tool_schemas(procedure: CrowdingProcedure) -> list[dict]:
    return [
        {
            "name": "list_unfiled_notes",
            "description": "List the notes still waiting to be filed (id and title).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "read_note",
            "description": "Read the full text of one note by id.",
            "inputSchema": {"type": "object", "properties": {"note_id": {"type": "string"}},
                            "required": ["note_id"]},
        },
        {
            "name": "file_note",
            "description": f"File a note into a folder. Folders: {', '.join(procedure.folders)}.",
            "inputSchema": {"type": "object",
                            "properties": {"note_id": {"type": "string"},
                                           "folder": {"type": "string", "enum": list(procedure.folders)}},
                            "required": ["note_id", "folder"]},
        },
    ]


class _Store:
    def __init__(self, state_file: Path | None):
        self.state_file = state_file
        self.filed: dict[str, str] = {}
        if state_file and state_file.exists():
            try:
                self.filed = json.loads(state_file.read_text()).get("filed", {})
            except (json.JSONDecodeError, OSError):
                self.filed = {}

    def save(self) -> None:
        if self.state_file:
            self.state_file.write_text(json.dumps({"filed": self.filed}) + "\n")


def _text_result(payload, *, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        result["isError"] = True
    return result


def _handle_call(procedure: CrowdingProcedure, store: _Store, name: str, args: dict) -> dict:
    notes = {n.id: n for n in procedure.notes}
    if name == "list_unfiled_notes":
        unfiled = [{"id": n.id, "title": n.title} for n in procedure.notes
                   if n.id not in store.filed]
        return _text_result({"unfiled": unfiled, "count": len(unfiled)})
    if name == "read_note":
        note = notes.get(args.get("note_id", ""))
        if note is None:
            return _text_result({"error": f"unknown note id {args.get('note_id')!r}"}, is_error=True)
        return _text_result({"id": note.id, "title": note.title, "body": note.body})
    if name == "file_note":
        note_id, folder = args.get("note_id", ""), args.get("folder", "")
        if note_id not in notes:
            return _text_result({"error": f"unknown note id {note_id!r}"}, is_error=True)
        if folder not in procedure.folders:
            return _text_result({"error": f"unknown folder {folder!r}; "
                                          f"folders: {list(procedure.folders)}"}, is_error=True)
        store.filed[note_id] = folder
        store.save()
        remaining = sum(1 for n in procedure.notes if n.id not in store.filed)
        return _text_result({"filed": note_id, "folder": folder, "remaining": remaining})
    return _text_result({"error": f"unknown tool {name!r}"}, is_error=True)


def serve(procedure: CrowdingProcedure, store: _Store, in_stream, out_stream) -> None:
    for raw in in_stream:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue
        method, msg_id = msg["method"], msg.get("id")
        if msg_id is None:
            continue  # notification (e.g. notifications/initialized)
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": procedure.server_name, "version": f"{procedure.version}"}}
            reply = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        elif method == "tools/list":
            reply = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_schemas(procedure)}}
        elif method == "tools/call":
            params = msg.get("params") or {}
            result = _handle_call(procedure, store, params.get("name", ""),
                                  params.get("arguments") or {})
            reply = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        else:
            reply = {"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32601, "message": f"method {method!r} not supported"}}
        out_stream.write(json.dumps(reply).encode() + b"\n")
        out_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--procedure", required=True)
    parser.add_argument("--state-file", default=None)
    args = parser.parse_args(argv)
    procedure = get_procedure(args.procedure)
    if procedure is None:
        sys.stderr.write(f"distractor: unknown crowding procedure {args.procedure!r}\n")
        return 2
    store = _Store(Path(args.state_file) if args.state_file else None)
    serve(procedure, store, sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
