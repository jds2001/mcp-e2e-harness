"""The loop driver's consumer: a minimal MCP client and tool loop, run as a subprocess.

50-drivers.md (loop): the harness supplies the consumer itself -- a minimal agent loop
over an OpenAI-compatible chat-completions endpoint -- so a cell measures server x
model under the pinned harness scaffold (``loop_scaffold.py``), not a shipping
product. The consumer is a subprocess for the same reason the product drivers are:
the recorder must observe actual outbound bytes (requirement 2), never a copy of what
the loop intended to send, so this process speaks HTTP to the recorder's localhost
URL exactly as a product CLI would, and the recorder forwards to the deployment.

Invoked by the loop driver, never by hand::

    python -m mcp_e2e_harness.loop_consumer --turn /path/loop-turn.json

The prompt arrives on stdin; the final assistant text is stdout and nothing else is;
progress and diagnostics go to stderr; the structured outcome lands in the
``result_path`` the turn file names. Exit codes: 0 answer produced; 2 harness/IO
failure; 3 instrument breach (served-provider mismatch, strict-routing refusal,
surface drift between session turns); 4 a consumer limit (ruling S13): the turn ended
without an answer because the consumer's own tool calls grew a request past the served
endpoint's context length and the endpoint refused it, or because the scaffold's step
cap ended the loop -- a consumer outcome, not a harness failure, recorded under
``consumer_limit`` in the result file.

Modes: ``turn`` runs the tool loop against the MCP servers in the harness-written
config (the SUT through the trace proxy, the distractor beside it in crowded cells);
``probe`` sends the scaffold's calibration probe -- one request offering only the
probe tool -- and judges the reply (requirement 9). Sessions (crowding pre-turn then
scored turn) share one conversation through a session file the ``open`` turn writes
and the ``resume`` turn continues.

The consumer reads full responses, as any consumer does; what reaches the artifacts
is the recorder's business (10-harness.md, recording contract), not this file's.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .loop_scaffold import LoopScaffold, get_scaffold, validate_probe_arguments
from .mcp_client import MCPClientError, StdioMCPClient
from .openrouter import EST_BYTES_PER_TOKEN, served_matches_pin

EXIT_OK, EXIT_ERROR, EXIT_BREACH, EXIT_LIMIT = 0, 2, 3, 4

_CONTEXT_MAX_RE = re.compile(r"maximum context length is (\d[\d,]*) tokens", re.I)
_CONTEXT_REQ_RE = re.compile(r"requested about (\d[\d,]*) tokens", re.I)

# OpenAI-compatible function names; a server tool name outside this set cannot be
# offered verbatim, which is an instrument limit reported loudly, never mangled.
_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_RETRY_STATUSES = (429, 502, 503, 504)
_RETRY_BACKOFF_S = (2.0, 5.0)


class LoopBreach(RuntimeError):
    """An instrument breach observed by the loop itself (the wire record is the
    admissible evidence; this is the fail-fast that stops spend)."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class LoopError(RuntimeError):
    """A harness/IO failure: not a consumer result."""


class LoopLimit(RuntimeError):
    """The consumer ran itself into a limit (ruling S13): a consumer outcome."""

    def __init__(self, cause: str, detail: str, record: dict):
        super().__init__(f"{cause}: {detail}")
        self.cause = cause
        self.detail = detail
        self.record = record


def wire_tool_name(server_name: str, tool_name: str) -> str:
    """The name a server tool is offered under on the wire -- the same idiom the
    claude-code driver's wire shows (``mcp__<server>__<tool>``), so surfaces compare
    across drivers and two servers' tools can never collide."""
    return f"mcp__{server_name}__{tool_name}"


@dataclass
class TurnConfig:
    api_base: str                 # recorder URL + deployment api prefix
    api_key_env: str
    model: str
    knobs: dict
    provider: dict                # the strict-routing block, sent verbatim on every request
    provider_pin: str | None
    pinned_provider_names: list[str] | None
    scaffold: str
    mode: str                     # "turn" | "probe"
    result_path: str
    mcp_config: str | None = None
    session_mode: str = "single"  # "single" | "open" | "resume"
    session_path: str | None = None
    request_timeout_s: float = 600.0
    server_order: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> TurnConfig:
        data = json.loads(path.read_text())
        return cls(**data)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cost_unread: int = 0

    def add(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            self.cost_unread += 1
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        if isinstance(details, dict):
            self.reasoning_tokens += int(details.get("reasoning_tokens") or 0)
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost_usd += float(cost)
        else:
            self.cost_unread += 1

    def as_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens, "cost_usd": round(self.cost_usd, 8),
                "cost_unread_responses": self.cost_unread}


def _log(message: str) -> None:
    sys.stderr.write(f"loop: {message}\n")
    sys.stderr.flush()


# ------------------------------------------------------------ HTTP to the recorder

class ChatClient:
    def __init__(self, config: TurnConfig, log=_log):
        self.config = config
        self.log = log
        key = os.environ.get(config.api_key_env, "").strip()
        if not key:
            raise LoopError(f"{config.api_key_env} is not set in the consumer's environment")
        self._key = key
        self.retries = 0
        # Turn state the S13 classification needs: tool calls so far in this turn and
        # the provider that served the previous response (the "served endpoint").
        self.turn_tool_calls = 0
        self.last_served: str | None = None

    def complete(self, body: dict) -> dict:
        """POST one chat completion; returns the parsed response. Retries only the
        statuses that mean 'try again' (each retry is a real request on the wire)."""
        url = self.config.api_base.rstrip("/") + "/chat/completions"
        data = json.dumps(body).encode("utf-8")
        attempt = 0
        while True:
            request = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json",
                         "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.config.request_timeout_s) as response:
                    raw = response.read()
                    status = response.status
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                status = exc.code
            except (urllib.error.URLError, OSError) as exc:
                raise LoopError(f"request to {url} failed: {exc}") from None
            if status in _RETRY_STATUSES and attempt < len(_RETRY_BACKOFF_S):
                self.retries += 1
                self.log(f"HTTP {status}; retrying in {_RETRY_BACKOFF_S[attempt]:.0f}s")
                time.sleep(_RETRY_BACKOFF_S[attempt])
                attempt += 1
                continue
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                payload = None
            if status >= 400:
                self._raise_for_error(status, payload, body)
            if not isinstance(payload, dict):
                raise LoopError(f"HTTP {status} with a non-JSON body ({len(raw)} bytes)")
            # OpenRouter reports some failures as 200 with an error object.
            if "error" in payload and not payload.get("choices"):
                self._raise_for_error(status, payload, body)
            return payload

    def _raise_for_error(self, status: int, payload: Any, body: dict) -> None:
        message = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                # OpenRouter puts the upstream's own words under metadata.raw
                # ("temporarily rate-limited upstream", ...) with the provider that
                # answered; "Provider returned error" alone hides what happened.
                meta = error.get("metadata")
                if isinstance(meta, dict):
                    if meta.get("provider_name"):
                        message += f" [provider {meta['provider_name']}]"
                    if meta.get("raw"):
                        message += f": {str(meta['raw'])[:300]}"
            elif error:
                message = str(error)
        sent_knobs = sorted(k for k in body if k in self.config.knobs)
        if status == 404 and body.get("provider", {}).get("require_parameters"):
            # Strict routing refused the request: no endpoint under the pin declares
            # every parameter sent. Name the knobs so the breach is legible, and the
            # ones the listing does not declare when the listing can be read.
            unsupported = self._undeclared_knobs(sent_knobs)
            named = (f"knobs not declared by the pinned endpoint: {unsupported}" if unsupported
                     else f"knobs sent: {sent_knobs}")
            raise LoopBreach(
                "routing_refused",
                f"the router refused the request under strict routing (HTTP {status}: "
                f"{message or 'no message'}); {named}. Under require_parameters the refusal is "
                "loud; without it the same knob would be silently dropped (P-knob-drop).")
        if 400 <= status < 500 and self.turn_tool_calls >= 1:
            limit = self._context_limit(status, message, body)
            if limit is not None:
                raise LoopLimit("context_length", limit["detail"], limit)
        raise LoopError(f"HTTP {status} from the endpoint: {message or 'no message'}")

    def _context_limit(self, status: int, message: str, body: dict) -> dict | None:
        """S13's mechanical rule: a 4xx on a request whose recorded size exceeds the
        served endpoint's listed context length, after at least one tool call, is a
        consumer limit. The size is the request body as sent, converted at the
        measured bytes-per-token ratio; the listed length is the pinned endpoint's, or
        the previously served provider's endpoint(s), from the deployment's listing.
        The endpoint's own numbers, when its message states them, are recorded beside
        the rule's inputs. None when the rule does not fire."""
        request_bytes = len(json.dumps(body).encode("utf-8"))
        estimated_tokens = round(request_bytes / EST_BYTES_PER_TOKEN)
        listed, source = self._listed_context_length()
        stated_max = _CONTEXT_MAX_RE.search(message or "")
        stated_req = _CONTEXT_REQ_RE.search(message or "")
        stated = {"max_tokens": int(stated_max.group(1).replace(",", "")) if stated_max else None,
                  "requested_tokens": int(stated_req.group(1).replace(",", "")) if stated_req else None}
        exceeds_listed = listed is not None and estimated_tokens > listed
        exceeds_stated = (stated["max_tokens"] is not None and stated["requested_tokens"] is not None
                          and stated["requested_tokens"] > stated["max_tokens"])
        if not (exceeds_listed or exceeds_stated):
            return None
        return {
            "cause": "context_length",
            "http_status": status,
            "endpoint_message": (message or "")[:600],
            "request_bytes": request_bytes,
            "estimated_request_tokens": estimated_tokens,
            "listed_context_length": listed,
            "listed_context_source": source,
            "endpoint_stated": stated,
            "tool_calls_before": self.turn_tool_calls,
            "rule": ("S13: 4xx on a request whose size exceeds the served endpoint's listed "
                     "context length, after >=1 tool call in the turn"),
            "detail": (f"HTTP {status}: the request (~{estimated_tokens:,} tokens from {request_bytes:,} "
                       f"bytes) exceeded the endpoint's context length "
                       f"({listed if listed is not None else 'unlisted'} listed"
                       + (f"; endpoint states {stated['requested_tokens']:,} > {stated['max_tokens']:,}"
                          if exceeds_stated else "")
                       + f") after {self.turn_tool_calls} tool call(s): a consumer outcome, not a harness failure"),
        }

    def _listed_context_length(self) -> tuple[int | None, str | None]:
        url = (self.config.api_base.rstrip("/")
               + f"/models/{urllib.parse.quote(self.config.model, safe='/')}/endpoints")
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as response:
                listing = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None, None
        endpoints = ((listing.get("data") or {}).get("endpoints") or []) if isinstance(listing, dict) else []
        lengths = [e.get("context_length") for e in endpoints if isinstance(e.get("context_length"), int)]
        if self.config.provider_pin:
            for e in endpoints:
                if e.get("tag") == self.config.provider_pin and isinstance(e.get("context_length"), int):
                    return e["context_length"], f"pinned endpoint {self.config.provider_pin}"
        if self.last_served:
            mine = [e.get("context_length") for e in endpoints
                    if e.get("provider_name") == self.last_served and isinstance(e.get("context_length"), int)]
            if mine:
                return max(mine), f"endpoint(s) of the previously served provider {self.last_served}"
        if lengths:
            return max(lengths), "largest listed endpoint (served endpoint unknown)"
        return None, None

    def _undeclared_knobs(self, sent_knobs: list[str]) -> list[str] | None:
        """Knobs the pinned endpoint's listing does not declare; None when unreadable."""
        pin = self.config.provider_pin
        if not pin:
            return None
        url = (self.config.api_base.rstrip("/")
               + f"/models/{urllib.parse.quote(self.config.model, safe='/')}/endpoints")
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=20) as response:
                listing = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        endpoints = ((listing.get("data") or {}).get("endpoints") or []) if isinstance(listing, dict) else []
        for endpoint in endpoints:
            if endpoint.get("tag") == pin:
                declared = set(endpoint.get("supported_parameters") or [])
                return [k for k in sent_knobs if k not in declared]
        return None


# ----------------------------------------------------------------- tool surface

@dataclass
class OfferedTool:
    wire_name: str
    server_name: str
    tool_name: str
    client: StdioMCPClient
    definition: dict


def open_servers(mcp_config_path: Path, log=_log) -> dict[str, StdioMCPClient]:
    """Spawn every server in the harness-written config (in config order) and
    initialize it. In a harness run the SUT entry is the trace proxy."""
    config = json.loads(mcp_config_path.read_text())
    clients: dict[str, StdioMCPClient] = {}
    for name, entry in (config.get("mcpServers") or {}).items():
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (entry.get("env") or {}).items()})
        client = StdioMCPClient(entry["command"], list(entry.get("args") or []), env=env, timeout=120)
        try:
            client.start()
        except MCPClientError as exc:
            for other in clients.values():
                other.close()
            raise LoopError(f"MCP server {name!r} failed to start: {exc}") from None
        clients[name] = client
        log(f"server {name}: up")
    return clients


def offer_tools(clients: dict[str, StdioMCPClient]) -> list[OfferedTool]:
    offered: list[OfferedTool] = []
    for name, client in clients.items():
        try:
            tools = client.list_tools()
        except MCPClientError as exc:
            raise LoopError(f"tools/list failed for server {name!r}: {exc}") from None
        for tool in tools:
            tool_name = tool.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            wire = wire_tool_name(name, tool_name)
            if not _FUNCTION_NAME_RE.match(wire):
                raise LoopError(f"tool name {wire!r} is not a valid function name for the endpoint; "
                                "the surface cannot be offered verbatim")
            definition = {
                "type": "function",
                "function": {
                    "name": wire,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                },
            }
            offered.append(OfferedTool(wire, name, tool_name, client, definition))
    return offered


def tool_result_text(record: dict) -> str:
    """The text handed back to the model for one MCP call (scaffold policy)."""
    response = record.get("response")
    if isinstance(response, dict):
        texts = [item.get("text") for item in (response.get("content") or [])
                 if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return json.dumps(response, sort_keys=True) if response is not None else ""


# ------------------------------------------------------------------- the loop

def build_request(config: TurnConfig, messages: list[dict], tools: list[dict]) -> dict:
    """One chat-completions body: harness-constructed fields plus the knobs verbatim.

    ``usage.include`` asks the deployment to return per-request cost (requirement 6);
    the strict-routing ``provider`` block is the cell's, unchanged on every request.
    """
    body: dict[str, Any] = {"model": config.model, "messages": messages}
    if tools:
        body["tools"] = tools
    body["provider"] = json.loads(json.dumps(config.provider))
    body["usage"] = {"include": True}
    for key, value in config.knobs.items():
        body[key] = value
    return body


def assistant_message(message: dict) -> dict:
    """The assistant turn as it is appended back into the conversation: role, content,
    tool calls, and the deployment's reasoning carrier when present (OpenRouter asks
    for ``reasoning_details`` to be echoed so reasoning models keep their state across
    tool calls). Nothing else from the response goes back."""
    out: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    if message.get("reasoning_details"):
        out["reasoning_details"] = message["reasoning_details"]
    return out


def check_served_provider(config: TurnConfig, response: dict, seq: int) -> str | None:
    served = response.get("provider")
    served = served if isinstance(served, str) and served else None
    if config.provider_pin and not served_matches_pin(served, config.provider_pin, config.pinned_provider_names):
        raise LoopBreach(
            "provider_mismatch",
            f"request {seq}: served provider {served!r} is not the pin {config.provider_pin!r} "
            "(a response lacking a provider is a mismatch, not a pass)")
    return served


def run_turn(config: TurnConfig, scaffold: LoopScaffold, prompt: str, client: ChatClient,
             log=_log) -> tuple[str, dict]:
    """Run one turn of the tool loop; returns (answer, result record)."""
    result: dict[str, Any] = {"mode": "turn", "scaffold": scaffold.full_name,
                              "scaffold_hash": scaffold.content_hash(), "steps": 0,
                              "tool_calls": 0, "step_cap": scaffold.step_cap, "step_cap_hit": False,
                              "served_providers": {}, "breach": None, "consumer_limit": None,
                              "session_mode": config.session_mode}
    usage = Usage()
    session: dict | None = None
    if config.session_mode == "resume":
        if not config.session_path or not Path(config.session_path).exists():
            raise LoopError(f"session file {config.session_path!r} missing: cannot resume a conversation "
                            "that was never opened, so the cell would not be crowded")
        session = json.loads(Path(config.session_path).read_text())
        messages = list(session["messages"])
    else:
        messages = [{"role": "system", "content": scaffold.system_prompt}]

    clients = open_servers(Path(config.mcp_config), log) if config.mcp_config else {}
    try:
        offered = offer_tools(clients)
        by_wire = {t.wire_name: t for t in offered}
        tools = [t.definition for t in offered]
        result["offered_tools"] = sorted(by_wire)
        if session is not None and sorted(session.get("offered_tools") or []) != sorted(by_wire):
            raise LoopBreach("surface_drift",
                             f"the resumed turn offers {sorted(by_wire)} but the session was opened with "
                             f"{sorted(session.get('offered_tools') or [])}")
        messages.append({"role": "user", "content": prompt})
        answer: str | None = None
        for step in range(1, scaffold.step_cap + 1):
            result["steps"] = step
            try:
                response = client.complete(build_request(config, messages, tools))
            except LoopLimit as limit:
                result["consumer_limit"] = limit.record
                log(f"CONSUMER LIMIT -- {limit.detail}")
                break
            usage.add(response.get("usage"))
            served = check_served_provider(config, response, step)
            client.last_served = served
            key = served or "(absent)"
            result["served_providers"][key] = result["served_providers"].get(key, 0) + 1
            choices = response.get("choices") or []
            message = (choices[0].get("message") if choices and isinstance(choices[0], dict) else None) or {}
            calls = message.get("tool_calls") or []
            log(f"step {step}: provider={served} tool_calls={len(calls)} "
                f"tokens={usage.prompt_tokens}/{usage.completion_tokens} cost=${usage.cost_usd:.6f}")
            messages.append(assistant_message(message))
            if not calls:
                content = message.get("content")
                answer = content if isinstance(content, str) else json.dumps(content)
                break
            for call in calls:
                result["tool_calls"] += 1
                client.turn_tool_calls += 1
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": _execute_call(call, by_wire, log)})
        if answer is None and result["consumer_limit"] is None:
            result["step_cap_hit"] = True
            result["consumer_limit"] = {
                "cause": "step_cap", "step_cap": scaffold.step_cap, "tool_calls": result["tool_calls"],
                "rule": "S13: the scaffold's step cap ended the loop before a final answer",
                "detail": (f"the step cap ({scaffold.step_cap} requests) ended the turn without a final "
                           "answer: a consumer outcome, not a harness failure"),
            }
    finally:
        for c in clients.values():
            c.close()
        result["usage"] = usage.as_dict()
        result["retries"] = client.retries
        if config.session_mode in ("open", "resume") and config.session_path:
            Path(config.session_path).write_text(json.dumps(
                {"messages": messages, "offered_tools": sorted(by_wire) if 'by_wire' in locals() else [],
                 "scaffold": scaffold.full_name}, indent=1) + "\n")
    return (answer or ""), result


def _execute_call(call: dict, by_wire: dict[str, OfferedTool], log) -> str:
    function = call.get("function") or {}
    name = function.get("name")
    raw_args = function.get("arguments")
    tool = by_wire.get(name)
    if tool is None:
        log(f"model called unknown tool {name!r}")
        return f"error: no tool named {name!r} is available"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except ValueError:
        log(f"unparseable arguments for {name}")
        return "error: arguments were not a valid JSON object"
    if not isinstance(args, dict):
        return "error: arguments were not a JSON object"
    try:
        record = tool.client.call_tool(tool.tool_name, args)
    except MCPClientError as exc:
        raise LoopError(f"tool call {tool.tool_name!r} failed at the MCP layer: {exc}") from None
    log(f"tool {name} -> {'error' if record['is_error'] else 'ok'} ({record['response_bytes']} bytes)")
    return tool_result_text(record)


# ------------------------------------------------------------------- the probe

def run_probe(config: TurnConfig, scaffold: LoopScaffold, client: ChatClient, log=_log) -> dict:
    """The calibration probe (requirement 9): one discarded request under the cell's
    exact knobs and pin, offering only the probe tool. Pass = exactly one tool call,
    the probe tool's name, JSON arguments valid against its schema."""
    result: dict[str, Any] = {"mode": "probe", "scaffold": scaffold.full_name,
                              "scaffold_hash": scaffold.content_hash(), "verdict": "broken",
                              "detail": None, "served_provider": None, "tool_calls": 0,
                              "tool_name": None, "arguments_raw": None, "schema_problems": None,
                              "breach": None}
    usage = Usage()
    messages = [{"role": "system", "content": scaffold.system_prompt},
                {"role": "user", "content": scaffold.probe_prompt}]
    try:
        response = client.complete(build_request(config, messages, [scaffold.probe_tool]))
    except LoopBreach as exc:
        result.update(verdict="fail", detail=str(exc), breach={"kind": exc.kind, "detail": exc.detail})
        return result
    finally:
        result["usage"] = usage.as_dict()
        result["retries"] = client.retries
    usage.add(response.get("usage"))
    result["usage"] = usage.as_dict()
    served = response.get("provider")
    result["served_provider"] = served if isinstance(served, str) else None
    try:
        check_served_provider(config, response, 1)
    except LoopBreach as exc:
        result.update(verdict="fail", detail=str(exc), breach={"kind": exc.kind, "detail": exc.detail})
        return result
    choices = response.get("choices") or []
    message = (choices[0].get("message") if choices and isinstance(choices[0], dict) else None) or {}
    calls = message.get("tool_calls") or []
    result["tool_calls"] = len(calls)
    if len(calls) != 1:
        result.update(verdict="fail",
                      detail=f"expected exactly one tool call, got {len(calls)}"
                             + ("" if calls else f"; answer text: {str(message.get('content'))[:200]!r}"))
        return result
    function = calls[0].get("function") or {}
    result["tool_name"] = function.get("name")
    result["arguments_raw"] = function.get("arguments")
    if function.get("name") != scaffold.probe_tool_name:
        result.update(verdict="fail", detail=f"called {function.get('name')!r}, not the offered "
                                             f"{scaffold.probe_tool_name!r}")
        return result
    raw = function.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        result.update(verdict="fail", detail=f"arguments are not valid JSON: {str(raw)[:200]!r}")
        return result
    problems = validate_probe_arguments(scaffold, args)
    result["schema_problems"] = problems
    if problems:
        result.update(verdict="fail", detail=f"arguments not valid against the schema: {problems}")
        return result
    result["arguments"] = args
    result.update(verdict="pass", detail=None)
    log(f"probe: pass (provider={served}, arguments={args})")
    return result


# ------------------------------------------------------------------------ main

def execute(config: TurnConfig, prompt: str, out=sys.stdout, log=_log) -> int:
    scaffold = get_scaffold(config.scaffold)
    if scaffold is None:
        log(f"unknown scaffold {config.scaffold!r}")
        return EXIT_ERROR
    result_path = Path(config.result_path)
    try:
        client = ChatClient(config, log)
        if config.mode == "probe":
            result = run_probe(config, scaffold, client, log)
            result_path.write_text(json.dumps(result, indent=2) + "\n")
            return EXIT_OK if result["verdict"] == "pass" else EXIT_ERROR
        answer, result = run_turn(config, scaffold, prompt, client, log)
    except LoopBreach as exc:
        result_path.write_text(json.dumps(
            {"mode": config.mode, "breach": {"kind": exc.kind, "detail": exc.detail}}, indent=2) + "\n")
        log(f"INSTRUMENT BREACH -- {exc}")
        return EXIT_BREACH
    except (LoopError, MCPClientError) as exc:
        result_path.write_text(json.dumps({"mode": config.mode, "error": str(exc)}, indent=2) + "\n")
        log(f"error: {exc}")
        return EXIT_ERROR
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    if result["consumer_limit"] is not None:
        log(f"consumer limit ({result['consumer_limit']['cause']}): no answer produced")
        return EXIT_LIMIT
    out.write(answer)
    out.flush()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turn", required=True, help="the loop turn file the driver wrote")
    args = parser.parse_args(argv)
    config = TurnConfig.load(Path(args.turn))
    prompt = sys.stdin.read() if config.mode == "turn" else ""
    return execute(config, prompt)


if __name__ == "__main__":
    sys.exit(main())
