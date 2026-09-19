"""A fake OpenRouter for loop-driver tests: catalog routes plus a scriptable model.

Serves the three free catalog reads the runner makes (``/api/v1/models``,
``/api/v1/models/<id>/endpoints``, ``/api/v1/providers``) and the chat-completions
endpoint the loop consumer speaks. The "model" is a script: a request whose last
message is from the user gets one tool call (to the configured tool, else the first
offered), a request whose last message is a tool result gets a final answer, and a
request offering only the probe tool gets the configured probe behavior. Every
chat-completions body received is kept verbatim in ``requests`` so tests can assert
what actually arrived on the wire (through the harness recorder). No network.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "fake/model-1"
PROVIDER = "FakeProvider"
PIN = "fakeprov/bf16"
PROBE_TOOL = "get_probe_value"

REFUSAL_MESSAGE = "No endpoints found that can handle the requested parameters"


class FakeOpenRouter:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.lock = threading.Lock()
        # --- scriptable behavior
        self.provider_sequence: list[str] = [PROVIDER]   # cycled per chat request
        self.omit_provider_from: int | None = None       # 1-based chat request index
        self.refuse_knobs: set[str] = set()              # 404 under require_parameters
        self.probe_behavior = "well-formed"
        self.runaway = False
        self.answer_text = "The tools say: done."
        self.tool_to_call: str | None = None
        self.tool_arguments = "{}"
        self.cost_per_request: float | None = 0.001
        self.status_sequence: list[int] = []             # forced statuses, consumed in order
        self.context_limit_tokens: int | None = None      # 400 once a request (bytes/2.83) exceeds it
        self.answer_finish_reason = "stop"                # finish_reason on a final answer
        self.echo_prompt_in_answer = False                # append the last user text to the answer
        self.reasoning_tokens = 3
        self.models = [{"id": MODEL, "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
                        "supported_parameters": ["tools", "max_tokens", "reasoning", "temperature"]}]
        self.endpoints = {MODEL: [
            {"provider_name": PROVIDER, "tag": PIN, "quantization": "bf16", "status": 0,
             "context_length": 131072,
             "supported_parameters": ["tools", "max_tokens", "reasoning"],
             "pricing": {"prompt": "0.00000003", "completion": "0.00000017"}},
            {"provider_name": "Other", "tag": "other/fp8", "quantization": "fp8", "status": 0,
             "context_length": 131072,
             "supported_parameters": ["tools", "max_tokens", "reasoning", "temperature"],
             "pricing": {"prompt": "0.00000005", "completion": "0.00000025"}},
        ]}
        self.providers = [
            {"name": PROVIDER, "slug": "fakeprov", "privacy_policy_url": "https://fake.test/privacy",
             "terms_of_service_url": "https://fake.test/terms", "status_page_url": None,
             "headquarters": "XX", "datacenters": ["XX"]},
            {"name": "Other", "slug": "other", "privacy_policy_url": None, "terms_of_service_url": None,
             "status_page_url": None, "headquarters": None, "datacenters": None},
        ]
        self._server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ARG002
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                with fake.lock:
                    fake.paths.append(self.path)
                path = self.path.split("?")[0]
                if path == "/api/v1/models":
                    return self._send(200, {"data": fake.models})
                if path == "/api/v1/providers":
                    return self._send(200, {"data": fake.providers})
                if path.startswith("/api/v1/models/") and path.endswith("/endpoints"):
                    model = path[len("/api/v1/models/"):-len("/endpoints")]
                    return self._send(200, {"data": {"id": model, "endpoints": fake.endpoints.get(model, [])}})
                return self._send(404, {"error": {"message": "not found", "code": 404}})

            def do_POST(self):
                with fake.lock:
                    fake.paths.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.path.split("?")[0] != "/api/v1/chat/completions":
                    return self._send(404, {"error": {"message": "not found", "code": 404}})
                body = json.loads(raw)
                status, payload = fake.answer(body)
                self._send(status, payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -------------------------------------------------------------- the model

    @property
    def chat_requests(self) -> list[dict]:
        return self.requests

    @property
    def probe_requests(self) -> list[dict]:
        return [r for r in self.requests if self._is_probe(r)]

    @property
    def scored_requests(self) -> list[dict]:
        return [r for r in self.requests if not self._is_probe(r)]

    @staticmethod
    def _is_probe(body: dict) -> bool:
        names = [t["function"]["name"] for t in body.get("tools") or []]
        return names == [PROBE_TOOL]

    def answer(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            self.requests.append(body)
            index = len(self.requests)
            forced = self.status_sequence.pop(0) if self.status_sequence else None
        if forced is not None and forced != 200:
            return forced, {"error": {"message": f"forced status {forced}", "code": forced}}
        provider = self.provider_sequence[(index - 1) % len(self.provider_sequence)]
        if self.omit_provider_from is not None and index >= self.omit_provider_from:
            provider = None
        if self.refuse_knobs & set(body) and (body.get("provider") or {}).get("require_parameters"):
            return 404, {"error": {"message": REFUSAL_MESSAGE, "code": 404}}
        if self.context_limit_tokens is not None:
            estimated = round(len(json.dumps(body).encode()) / 2.83)
            if estimated > self.context_limit_tokens:
                # The deny-r05 shape (2026-09-18): the endpoint refuses a request the
                # consumer's own tool results grew past its context length.
                return 400, {"error": {"message": (
                    f"This endpoint's maximum context length is {self.context_limit_tokens} tokens. "
                    f"However, you requested about {estimated} tokens. Please reduce the length."),
                    "code": 400}}
        tools = body.get("tools") or []
        names = [t["function"]["name"] for t in tools]
        messages = body.get("messages") or []
        if self._is_probe(body):
            message = self._probe_message()
        elif self.runaway or not messages or messages[-1].get("role") != "tool":
            target = self.tool_to_call if self.tool_to_call in names else next(
                (n for n in names if n.endswith("list_unfiled_notes")), names[0] if names else None)
            if target is None:
                message = {"role": "assistant", "content": "No tools to call."}
            else:
                message = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": f"call_{index}", "type": "function",
                                           "function": {"name": target, "arguments": self.tool_arguments}}]}
        else:
            text = self.answer_text
            if self.echo_prompt_in_answer:
                last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
                text = f"{text} [{last_user}]"
            message = {"role": "assistant", "content": text}
        usage = {"prompt_tokens": 100 + 10 * len(messages), "completion_tokens": 20,
                 "completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens}}
        if self.cost_per_request is not None:
            usage["cost"] = self.cost_per_request
        payload = {"id": f"gen-{index}", "model": body.get("model"), "object": "chat.completion",
                   "choices": [{"index": 0, "message": message,
                                "finish_reason": ("tool_calls" if message.get("tool_calls")
                                                  else self.answer_finish_reason)}],
                   "usage": usage}
        if provider is not None:
            payload["provider"] = provider
        return 200, payload

    def _probe_message(self) -> dict:
        call = {"id": "call_probe", "type": "function",
                "function": {"name": PROBE_TOOL, "arguments": json.dumps({"key": "alpha"})}}
        behavior = self.probe_behavior
        if behavior == "well-formed":
            return {"role": "assistant", "content": None, "tool_calls": [call]}
        if behavior == "no-call":
            return {"role": "assistant", "content": "The value under alpha is 42."}
        if behavior == "two-calls":
            return {"role": "assistant", "content": None, "tool_calls": [call, dict(call, id="call_2")]}
        if behavior == "wrong-name":
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c", "type": "function",
                                    "function": {"name": "lookup_value", "arguments": "{\"key\": \"alpha\"}"}}]}
        if behavior == "bad-json":
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c", "type": "function",
                                    "function": {"name": PROBE_TOOL, "arguments": "{key: alpha"}}]}
        if behavior == "schema-invalid":
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c", "type": "function",
                                    "function": {"name": PROBE_TOOL, "arguments": "{\"key\": 7, \"x\": 1}"}}]}
        raise AssertionError(f"unknown probe behavior {behavior!r}")
