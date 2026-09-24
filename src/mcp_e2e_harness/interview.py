"""Unscored, sequential follow-ups on immutable loop row evidence (S21)."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .api_capture import ApiSurfaceRecorder, read_records, surface_mismatches
from .drivers.loop import LoopDriver
from .loop_consumer import ChatClient, LoopError, LoopLimit, TurnConfig, open_servers, run_turn
from .loop_scaffold import get_scaffold
from .openrouter import EST_BYTES_PER_TOKEN, CatalogError


def _read(path: Path):
    return json.loads(path.read_text())


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def load_row(row: Path) -> tuple[dict, dict, Path, dict]:
    """Validate eligibility and replay identity before creating any artifacts."""
    meta = _read(row / "meta.json")
    if meta.get("driver_family") != "loop":
        raise ValueError("interview requires a loop row; product-driver rows cannot be resumed")
    for key in ("harness_failure", "precondition_unmet", "upstream_unavailable"):
        if meta.get(key) is not None:
            raise ValueError(f"row is ineligible: {key}: {meta[key]}")
    if "harness_failure" not in meta or meta.get("scored_turn_reached") is False:
        raise ValueError("row has no recorded scored-turn end")
    turn = _read(row / "loop-turn.json")
    session_path = row / Path(turn["session_path"]).name
    if not session_path.is_file():
        raise ValueError(f"session file missing: {session_path}")
    session = _read(session_path)
    scaffold = get_scaffold(turn["scaffold"])
    if (scaffold is None or meta.get("scaffold") != {
            "name": scaffold.full_name, "content_hash": scaffold.content_hash()}
            or session.get("scaffold") != scaffold.full_name):
        raise ValueError("recorded scaffold hash/name does not match the installed scaffold")
    if (turn["model"] != meta["model"] or turn["knobs"] != meta["knobs"]
            or (turn.get("provider_pin") or "unpinned") != meta["provider_pin"]
            or turn.get("provider", {}).get("require_parameters") is not True):
        raise ValueError("recorded turn disagrees with row model, pin, knobs, or strict routing")
    if not isinstance(session.get("messages"), list) or not session["messages"]:
        raise ValueError("recorded session has no messages")
    _read(row / "mcp-config.json")
    return meta, turn, session_path, session


def _directory(row: Path) -> Path:
    parent = row / "interview"
    parent.mkdir(exist_ok=True)
    index = 1
    while True:
        dest = parent / f"{index:02d}"
        try:
            dest.mkdir()
            return dest
        except FileExistsError:
            index += 1


def _server_config(row: Path, dest: Path) -> Path:
    """Relocate every harness output; never reuse a scored server's mutable state."""
    config = _read(row / "mcp-config.json")
    output_flags = {"--trace-file", "--tools-file", "--meta-file", "--state-file"}
    for entry in config["mcpServers"].values():
        args = entry.get("args", [])
        for i, arg in enumerate(args[:-1]):
            if arg in output_flags:
                args[i + 1] = str(dest / Path(args[i + 1]).name)
            elif arg == "--server-config":
                source = row / Path(args[i + 1]).name
                target = dest / source.name
                target.write_bytes(source.read_bytes())
                args[i + 1] = str(target)
    path = dest / "mcp-config.json"
    _write(path, config)
    return path


class InterviewClient(ChatClient):
    """Check the interview-only cap before every HTTP request, including tool rounds."""

    def __init__(self, config, budget, wire_path, log):
        super().__init__(config, log)
        self.budget = budget
        self.wire_path = wire_path

    def before_request(self):
        records = read_records(self.wire_path)
        spend = sum(r.get("usage_cost") or 0 for r in records)
        if self.budget is not None and spend >= self.budget:
            raise LoopLimit("budget", "interview budget reached", {"cause": "budget", "spend_usd": spend})


def interview(row: Path, questions: list[str], budget_usd: float | None = None, log=print) -> Path:
    if not questions or any(not isinstance(q, str) for q in questions):
        raise ValueError("provide at least one --ask or a JSON array of strings via --ask-file")
    if budget_usd is not None and (not math.isfinite(budget_usd) or budget_usd < 0):
        raise ValueError("budget must be finite and nonnegative")
    row = row.resolve()
    meta, turn, source, session = load_row(row)
    driver = LoopDriver()
    # Validate credentials before making a directory, without recording their value.
    config = TurnConfig(**turn)
    ChatClient(config, log)
    dest = _directory(row)
    log(f"interview  : {dest}")
    session_path = dest / "interview-session.json"
    session_path.write_bytes(source.read_bytes())
    _write(dest / "questions.json", [{"index": i, "question": q} for i, q in enumerate(questions, 1)])
    wire_path = dest / "api-surface.jsonl"
    wire_path.touch()
    (dest / "trace.jsonl").touch()
    run_dir = next((p for p in row.parents if (p / "run-manifest.json").exists()), row.parent)
    reasoning = any(m.get("reasoning") or m.get("reasoning_details") for m in session["messages"])
    record = {
        "interview": True, "aggregate": False, "row": row.relative_to(run_dir).as_posix(),
        "answer_sha256_16": meta.get("answer_sha256_16"), "repetition": meta.get("repetition"),
        "session_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "scaffold": meta["scaffold"], "model": meta["model"], "provider_pin": meta["provider_pin"],
        "knobs": meta["knobs"], "provider_policy": turn["provider"],
        "reasoning_replayed": None,
        "reasoning_replayed_note": "No request sent yet.",
        "tool_definitions_source": ("recorded session" if "tool_definitions" in session else
                                    "legacy session records names only; definitions from fresh server, unverifiable"),
        "questions": [], "spend_usd": 0.0, "budget_usd": budget_usd, "budget_stop": None,
    }
    if "tool_definitions" not in session:
        log("replay     : legacy session records tool names only; original definitions cannot be verified")
    _write(dest / "meta.json", record)
    old_wire = read_records(row / "api-surface.jsonl")
    input_tokens = next((r["usage_prompt_tokens"] for r in reversed(old_wire)
                         if isinstance(r.get("usage_prompt_tokens"), (int, float))), None)
    try:
        pricing = driver.catalog().model_pricing(config.model)
    except CatalogError:
        pricing = None
    estimate = None
    if input_tokens is not None and pricing:
        output_tokens = config.knobs.get("max_tokens", config.knobs.get("max_completion_tokens"))
        if not isinstance(output_tokens, (int, float)):
            output_tokens = 1000
        # Each question resends the prefix and the estimated exchange accumulated so far.
        tokens = sum(input_tokens + sum(len(q.encode()) / EST_BYTES_PER_TOKEN for q in questions[:i + 1])
                     + i * output_tokens for i in range(len(questions)))
        estimate = tokens * float(pricing["prompt"]) + len(questions) * output_tokens * float(pricing["completion"])
    record["estimate"] = {"usd": estimate, "prefix_prompt_tokens": input_tokens,
                          "pricing_per_token": pricing,
                          "basis": "every question resends row final usage_prompt_tokens plus exchange so far; "
                                   "tool rounds and reasoning tokens are unbounded estimates"}
    log(f"ESTIMATE   : {f'${estimate:.6f}' if estimate is not None else 'unavailable'}; "
        f"each question resends {input_tokens} prefix input tokens plus exchange so far; "
        "tool rounds and reasoning add spend. Cap checked before each request from actual usage.cost.")
    recorder = ApiSurfaceRecorder(wire_path, driver.upstream())
    clients = {}
    try:
        config.api_base = recorder.start().rstrip("/") + "/api/v1"
        config.mcp_config = str(_server_config(row, dest))
        config.session_mode = "resume"
        config.session_path = str(session_path)
        config.result_path = str(dest / "loop-result.json")
        if budget_usd != 0:
            clients = open_servers(Path(config.mcp_config), log)
        for index, question in enumerate(questions, 1):
            item = {"index": index, "asked": False, "finish_reason": None, "tool_calls": 0,
                    "served_providers": [], "spend_usd": 0.0, "consumer_limit": None}
            record["questions"].append(item)
            if record["budget_stop"] or (budget_usd is not None and record["spend_usd"] >= budget_usd):
                record["budget_stop"] = {"cause": "budget", "spend_usd": record["spend_usd"]}
                item["not_asked_reason"] = "budget"
                continue
            log(f"question {index}: {question}")
            start = len(read_records(wire_path))
            client = InterviewClient(config, budget_usd, wire_path, log)
            answer, result = run_turn(config, get_scaffold(config.scaffold), question, client, log,
                                      servers=clients, interview=True)
            lines = read_records(wire_path)[start:]
            item.update(asked=bool(lines), answer=answer, finish_reason=result.get("finish_reason"),
                        tool_calls=result.get("tool_calls", 0),
                        served_providers=[{"seq": r["seq"], "provider": r.get("provider")} for r in lines],
                        spend_usd=round(sum(r.get("usage_cost") or 0 for r in lines), 8),
                        consumer_limit=result.get("consumer_limit"), error=result.get("error"),
                        breach=result.get("breach"), usage=result.get("usage"))
            record["spend_usd"] = round(record["spend_usd"] + item["spend_usd"], 8)
            if lines:
                record["reasoning_replayed"] = True if reasoning else None
                record["reasoning_replayed_note"] = (
                    "All recorded message fields, including reasoning and reasoning_details, sent verbatim; "
                    "endpoint refusal, if any, is recorded per question." if reasoning else
                    "No reasoning content was present in the recorded prefix; messages sent verbatim.")
            if (result.get("consumer_limit") or {}).get("cause") == "budget":
                record["budget_stop"] = result["consumer_limit"]
            log(f"answer {index}: {answer}" if answer else
                f"answer {index}: {result.get('error') or result.get('consumer_limit')}")
            _write(dest / "meta.json", record)
    except (LoopError, OSError, ValueError) as exc:
        record["harness_failure"] = str(exc)
        log(f"interview error: {exc}")
    finally:
        for client in clients.values():
            client.close()
        recorder.stop()
        lines = read_records(wire_path)
        expected = session.get("offered_tools", [])
        for index in range(len(record["questions"]) + 1, len(questions) + 1):
            record["questions"].append({
                "index": index, "asked": False, "finish_reason": None, "tool_calls": 0,
                "served_providers": [], "spend_usd": 0.0, "consumer_limit": None,
                "not_asked_reason": record.get("harness_failure", "interrupted"),
            })
        record["api_surface"] = {
            "expected_wire_surface": expected,
            "wire_surface_mismatches": surface_mismatches(lines, expected),
            "requests": len(lines), "verified": bool(lines) and not recorder.unrecorded_requests
            and not surface_mismatches(lines, expected),
            "unparseable_requests": recorder.unrecorded_requests,
            "upstream_forward_errors": recorder.forward_errors,
        }
        _write(dest / "meta.json", record)
    log(f"spend      : ${record['spend_usd']:.6f}" + ("; BUDGET STOP" if record["budget_stop"] else ""))
    return dest
