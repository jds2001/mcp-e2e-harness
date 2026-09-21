"""API-boundary tool-surface capture: ground truth off the wire, not a model claim.

Q2's preferred instrument (documentation/90-open-questions.md, ruling 455568a): the
driver's outbound API traffic is routed through this transparent recording proxy, and
the harness records the actual ``tools`` array sent to the model on every request.
That makes the builtins' absence an *observation* rather than an argv assertion, with
zero disclosure -- recording an outgoing request changes nothing in the consumer's
context -- so it runs during scored cells, continuously, unlike a calibration probe.
Nothing model-mediated sits in the verification path.

What is recorded, per request that carries a body (one JSONL line): ``seq``,
``at``, ``path``, ``model``, ``tool_names``, ``tool_source``, ``body_keys``, and -- the
context-accounting fields (spec 10-harness.md, "The wire recorder's recording
contract", added 2026-09-02 for measurements such as uscde-mcp's E15) -- ``body_bytes``
with ``body_bytes_basis`` (``"decompressed"`` when the body parsed, after undoing a
gzip/deflate content-encoding if present; ``"wire"`` when it did not, in which case
the size is the raw wire bytes) and ``content_encoding`` (the request's declared
encoding, null for identity; the one header value the contract names), and -- timing
(spec 10-harness.md, added 2026-09-18 from WO-1 finding 5) -- ``duration_ms`` from
request arrival (``at``) to the end of the response, null with ``duration_note`` when
the response never completed. Chat-completion lines also carry ``http_status``
(WO-8), null if no status was received. Timing and status are properties of the
exchange, not response-body extractions, so they are not allowlist entries. Every line is therefore
written when its response ends (``seq`` is still assigned at arrival). Never any
other header (credentials ride there), never message content, never tool schemas.
Body-less requests (GETs) are forwarded and not recorded.

Response bodies are read only through ``RESPONSE_SCALAR_ALLOWLIST``: named scalar
extractions per allowlisted endpoint, recorded on that request's own line. Two
endpoints are allowlisted: the count-tokens endpoint (one scalar, ``count_tokens``,
the numeric count, present only when the driver itself made that call) and -- added
2026-09-18 for the loop driver (10-harness.md, recording contract; 50-drivers.md loop
requirements 4 and 6) -- the OpenAI-compatible chat-completions endpoint, with five
scalars: the served ``provider`` string and four usage numbers (``usage.prompt_tokens``,
``usage.completion_tokens``, ``usage.completion_tokens_details.reasoning_tokens``,
``usage.cost``), recorded under ``provider`` and ``usage_prompt_tokens`` /
``usage_completion_tokens`` / ``usage_reasoning_tokens`` / ``usage_cost``; and -- the
sixth entry, added 2026-09-18 for WO-3 (a scorer separating cap-cut from self-cut rows
by the recorded reason, not by token-count inference) -- ``choices[0].finish_reason``
under ``finish_reason``. Each is null with a ``<key>_note`` when absent or unreadable. Extraction decodes whatever
response encoding the driver negotiated; an encoding the recorder cannot decode
leaves the scalar null with the encoding named and is counted in the digest as an
unread call -- a failed read is never recorded as an absent call (DR-3). Nothing
else from any response body, ever. Growing the allowlist is a spec commit
(10-harness.md), never an implementation convenience. Every other response is
forwarded without being looked at.

Forwarding is streaming (chunked pass-through), so SSE responses reach the driver as
they arrive; the proxy must never change the timing shape enough to alter driver
behavior. Forwarding never depends on parsing: an unparseable body is forwarded
verbatim.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import zlib
from dataclasses import dataclass

import brotli

try:  # zstd: stdlib on 3.14+, the zstandard package otherwise; optional either way
    from compression import zstd as _zstd  # type: ignore[import-not-found]
    def _zstd_decompress(data: bytes) -> bytes:
        return _zstd.decompress(data)
except ImportError:  # pragma: no cover - depends on the interpreter
    try:
        import zstandard as _zstandard  # type: ignore[import-not-found]
        def _zstd_decompress(data: bytes) -> bytes:
            return _zstandard.ZstdDecompressor().decompressobj().decompress(data)
    except ImportError:
        _zstd_decompress = None  # type: ignore[assignment]
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def _extract_tool_names(payload: dict) -> tuple[list[str] | None, str | None]:
    """The tool surface a request declares, wherever this API dialect carries it.

    Three measured channels (2026-09-01; chat-completions shape 2026-09-18):

    * ``tools`` array (standard Responses/Messages shape). name-or-type: hosted tools
      (web_search and kin) carry a ``type`` and no ``name``; recording only names
      would blind the disallowed check to exactly the tools it exists to catch. The
      OpenAI-compatible chat-completions shape the loop driver speaks nests the name:
      ``{"type": "function", "function": {"name": ...}}`` -- that name is taken, and
      only the name (the schema under ``parameters`` is never read).
    * ``client_metadata["x-codex-turn-metadata"].code_mode_tool_names`` -- codex-family
      models ("code mode") send NO tools array even in a working tool-calling turn;
      the roster the backend-injected harness serves travels here as a name->
      {name, namespace} map. Measured by full-body probe: the mcp__* tools the
      consumer actually called appear only in this map.

    Returns (names, source) -- (None, None) when the request declares no surface.
    Only key names are ever taken; never schemas or content.
    """
    tools = payload.get("tools")
    if isinstance(tools, list):
        names = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            function = t.get("function")
            if t.get("type") == "function" and isinstance(function, dict) and function.get("name"):
                names.append(function["name"])
            else:
                names.append(t.get("name") or t.get("type"))
        return [n for n in names if n], "tools"
    meta = payload.get("client_metadata")
    if isinstance(meta, dict):
        turn_meta = meta.get("x-codex-turn-metadata")
        if isinstance(turn_meta, str):
            try:
                turn_meta = json.loads(turn_meta)
            except json.JSONDecodeError:
                turn_meta = None
        if isinstance(turn_meta, dict):
            code_mode = turn_meta.get("code_mode_tool_names")
            if isinstance(code_mode, dict):
                return sorted(code_mode.keys()), "code_mode_tool_names"
    return None, None


@dataclass(frozen=True)
class ScalarExtraction:
    """One allowlisted response scalar: the record key it lands under, the path into
    the response JSON object (string keys into objects, integers into arrays), and
    whether it is a number or a string."""
    key: str
    pointer: tuple[str | int, ...]
    kind: str  # "number" | "string"

    @property
    def path(self) -> str:
        out = ""
        for token in self.pointer:
            out += f"[{token}]" if isinstance(token, int) else (f".{token}" if out else token)
        return out


@dataclass(frozen=True)
class AllowlistedEndpoint:
    suffixes: tuple[str, ...]
    scalars: tuple[ScalarExtraction, ...]


# The response-side allowlist (10-harness.md, recording contract). Entry name ->
# (endpoint path suffixes, the named scalars read from that endpoint's responses).
#
# ``count_tokens`` covers the count-tokens endpoint in both dialects the product
# drivers speak: Anthropic Messages (``/v1/messages/count_tokens``) and OpenAI
# Responses (``/v1/responses/input_tokens``); both return the count under
# ``input_tokens``. ``chat_completions`` (2026-09-18) covers the OpenAI-compatible
# chat-completions endpoint the loop driver speaks: the served provider and four usage
# scalars, needed for served-provider identity per request and per-request cost
# (50-drivers.md loop #4/#6; measurement: openrouter-probe-2026-09-18.md).
#
# Adding an entry here without the spec commit that names the endpoint, the scalar,
# and the measurement that needed it is a contract breach.
RESPONSE_SCALAR_ALLOWLIST: dict[str, AllowlistedEndpoint] = {
    "count_tokens": AllowlistedEndpoint(
        suffixes=("/v1/messages/count_tokens", "/v1/responses/input_tokens"),
        scalars=(ScalarExtraction("count_tokens", ("input_tokens",), "number"),),
    ),
    "chat_completions": AllowlistedEndpoint(
        suffixes=("/chat/completions",),
        scalars=(
            ScalarExtraction("provider", ("provider",), "string"),
            ScalarExtraction("usage_prompt_tokens", ("usage", "prompt_tokens"), "number"),
            ScalarExtraction("usage_completion_tokens", ("usage", "completion_tokens"), "number"),
            ScalarExtraction("usage_reasoning_tokens",
                             ("usage", "completion_tokens_details", "reasoning_tokens"), "number"),
            ScalarExtraction("usage_cost", ("usage", "cost"), "number"),
            ScalarExtraction("finish_reason", ("choices", 0, "finish_reason"), "string"),
        ),
    ),
}

# Buffering cap for an allowlisted response: count-tokens responses are a few dozen
# bytes and a non-streamed chat completion is the assistant's output (answer text,
# tool-call arguments, reasoning carrier) -- kilobytes; anything past this is not the
# endpoint we think it is and the scalars go unread with the cap named.
_ALLOWLISTED_RESPONSE_CAP = 8 << 20


def _allowlist_entry(path: str) -> str | None:
    """The allowlist entry an endpoint path falls under, or None (the common case)."""
    bare = path.split("?")[0].rstrip("/")
    for name, endpoint in RESPONSE_SCALAR_ALLOWLIST.items():
        if any(bare.endswith(suffix) for suffix in endpoint.suffixes):
            return name
    return None


def _decode_body(body: bytes, content_encoding: str | None) -> bytes | None:
    """Undo the negotiated content-encoding; None when it cannot be undone.

    The recorder never narrows the driver's Accept-Encoding (that would modify
    traffic), so it must decode whatever the driver negotiates: gzip, deflate, brotli
    (DR-3, 2026-09-16 -- the encoding the upstream actually answered every observed
    count-tokens call with), and zstd where a codec is importable. Anything else
    stays opaque: recorded at wire size with the encoding named, per the contract. A
    mislabeled identity body (declared gzip, actually plain JSON) also comes back
    None here and is then tried as-is.
    """
    encoding = (content_encoding or "identity").strip().lower()
    try:
        if encoding in ("identity", ""):
            return body
        if encoding in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate, seen in the wild
        if encoding == "br":
            return brotli.decompress(body)
        if encoding == "zstd" and _zstd_decompress is not None:
            return _zstd_decompress(body)
    except (OSError, EOFError, zlib.error, brotli.error, ValueError):
        return None
    return None


def _parse_json_object(body: bytes, content_encoding: str | None) -> tuple[dict | None, bytes | None]:
    """(payload, decoded bytes) for a JSON-object body; (None, None) when unparseable."""
    candidates = [_decode_body(body, content_encoding)]
    if candidates[0] is None or candidates[0] is not body:
        candidates.append(body)  # a mislabeled or unknown encoding may still be plain JSON
    for decoded in candidates:
        if decoded is None:
            continue
        try:
            payload = json.loads(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload, decoded
    return None, None


def _extract_scalar(payload: dict, scalar: ScalarExtraction) -> tuple[int | float | str | None, str | None]:
    """One allowlisted scalar from a parsed response object; (None, why) when absent."""
    node: object = payload
    for token in scalar.pointer:
        if isinstance(token, int):
            if not isinstance(node, list) or not (0 <= token < len(node)):
                return None, f"response carries no {scalar.path!r}"
        elif not isinstance(node, dict) or token not in node:
            return None, f"response carries no {scalar.path!r}"
        node = node[token]
    if scalar.kind == "number":
        if isinstance(node, bool) or not isinstance(node, (int, float)):
            return None, f"response carries no numeric {scalar.path!r}"
        return node, None
    if not isinstance(node, str) or not node:
        return None, f"response carries no string {scalar.path!r}"
    return node, None


# Hop-by-hop headers; everything else is forwarded verbatim in both directions.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


class ApiSurfaceRecorder:
    """A localhost HTTP server that records tool surfaces and forwards to ``upstream``."""

    def __init__(self, record_file: Path, upstream: str, mark: dict | None = None):
        self.record_file = record_file
        self.upstream = urlsplit(upstream)
        # Harness-configured stamps on every line this recorder writes -- e.g.
        # ``{"probe": true}`` for the loop driver's calibration probe, whose request
        # goes through this same recorder but is never a scored request. The stamp
        # is configuration, not something read off the bytes.
        self.mark = dict(mark or {})
        if self.upstream.scheme not in ("http", "https") or not self.upstream.netloc:
            raise ValueError(f"upstream must be an http(s) URL, got {upstream!r}")
        self._lock = threading.Lock()
        self._seq = 0
        self.unrecorded_requests = 0
        self.unrecorded_encodings: set[str] = set()
        self.forward_errors = 0
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "recorder not started"
        host, port = self._server.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def start(self) -> str:
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: ARG002
                pass  # never write request lines (they could carry query secrets) anywhere

            def _handle(self) -> None:
                recorder._proxy(self)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):  # noqa: ARG002
                # Keep-alive sockets reset by the driver between requests are normal
                # (measured with codex 2026-09-01); a traceback per reset floods the
                # operator's terminal with noise that reads like instrument failure.
                import sys as _sys
                exc = _sys.exception()
                if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
                    return
                super().handle_error(request, client_address)

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---------------------------------------------------------------- recording

    def _record(self, path: str, body: bytes, content_encoding: str | None) -> dict | None:
        """One JSONL line per request that carries a body; unparseable bodies included.

        A request with no ``tools`` array is recorded with ``tool_names: null``
        (measured 2026-09-01: codex sends tool-less requests as part of a working
        turn, and recording only tools-carrying requests made "routed but tool-less"
        indistinguishable from "never routed" -- an instrument reading its own gap as
        a driver failure). ``body_keys`` names the request's top-level keys so an
        alternate tool-delivery channel is visible; still never headers, message
        content, or schemas.

        Context accounting (spec, 2026-09-02): every record carries ``body_bytes``.
        A body that parses (after undoing gzip/deflate) is measured decompressed --
        the serialized size that actually enters the model's context -- and marked
        ``body_bytes_basis: "decompressed"``; one that does not is still recorded,
        at wire size with ``body_bytes_basis: "wire"`` and its ``content_encoding``
        named, and counted in ``unrecorded_requests``/``unrecorded_encodings`` so the
        runner can say the surface went unread for it.

        Returns the record; the caller writes it once the response has ended (for
        ``duration_ms``, and for the allowlisted scalars where the endpoint has any).
        ``seq`` is assigned here, at request arrival.
        """
        payload, decoded = _parse_json_object(body, content_encoding)
        with self._lock:
            self._seq += 1
            seq = self._seq
            if payload is None:
                self.unrecorded_requests += 1
                self.unrecorded_encodings.add(content_encoding or "identity")
        record = {
            "seq": seq,
            "at": datetime.now(timezone.utc).isoformat(),
            "path": path.split("?")[0],
            "model": payload.get("model") if payload is not None else None,
            "tool_names": None,
            "tool_source": None,
            "body_keys": sorted(payload.keys()) if payload is not None else None,
            "body_bytes": len(decoded) if decoded is not None else len(body),
            "body_bytes_basis": "decompressed" if decoded is not None else "wire",
            "content_encoding": content_encoding or None,
        }
        if payload is not None:
            record["tool_names"], record["tool_source"] = _extract_tool_names(payload)
        record.update(self.mark)
        record["_t0"] = time.monotonic()
        return record

    def _write(self, record: dict) -> None:
        with self._lock, open(self.record_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()

    # ---------------------------------------------------------------- forwarding

    def _read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        length = handler.headers.get("Content-Length")
        if length is not None:
            return handler.rfile.read(int(length))
        if (handler.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks = []
            while True:
                size_line = handler.rfile.readline().strip()
                size = int(size_line.split(b";")[0], 16)
                if size == 0:
                    handler.rfile.readline()
                    break
                chunks.append(handler.rfile.read(size))
                handler.rfile.readline()
            return b"".join(chunks)
        return b""

    def _proxy(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._read_body(handler)
        pending: dict | None = None  # the request's record, written when its response ends
        if body:
            pending = self._record(handler.path, body, handler.headers.get("Content-Encoding"))
        entry = _allowlist_entry(handler.path) or ""

        conn_cls = HTTPSConnection if self.upstream.scheme == "https" else HTTPConnection
        conn = conn_cls(self.upstream.netloc, timeout=600)
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["Host"] = self.upstream.netloc
        headers["Connection"] = "close"
        path = (self.upstream.path.rstrip("/") + handler.path) if self.upstream.path else handler.path
        try:
            conn.request(handler.command, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            with self._lock:
                self.forward_errors += 1
            handler.send_response(502)
            message = json.dumps({"error": f"api-capture proxy could not reach upstream: {exc}"}).encode()
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(message)))
            handler.end_headers()
            handler.wfile.write(message)
            if pending is not None:
                self._finish(pending, entry, None, None, None, "upstream unreachable",
                             completed=False, incomplete_note="upstream unreachable")
            return
        # Only an allowlisted endpoint's response is buffered -- every other response
        # streams through untouched and unread.
        buffered: bytearray | None = bytearray() if (pending is not None and entry) else None
        overflow = False
        completed = False
        try:
            handler.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in _HOP_BY_HOP:
                    handler.send_header(key, value)
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                if buffered is not None:
                    if len(buffered) + len(chunk) <= _ALLOWLISTED_RESPONSE_CAP:
                        buffered += chunk
                    else:
                        overflow = True
                handler.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
            completed = True
        except OSError:
            pass  # driver hung up mid-stream; nothing to salvage
        finally:
            conn.close()
            if pending is not None:
                self._finish(
                    pending, entry, resp.status, bytes(buffered or b""),
                    resp.getheader("Content-Encoding"),
                    f"response exceeded {_ALLOWLISTED_RESPONSE_CAP} bytes" if overflow else None,
                    completed=completed,
                    incomplete_note=None if completed else "response did not complete (client closed the stream)")

    def _finish(self, record: dict, entry: str, status: int | None,
                response_body: bytes | None, content_encoding: str | None,
                note: str | None, *, completed: bool, incomplete_note: str | None) -> None:
        """Attach timing and, for an allowlisted endpoint, its scalars (or why each is
        absent); then write the held line.

        ``duration_ms`` runs from request arrival to the end of the response; when the
        response never completed it is null and ``duration_note`` says why. Each
        allowlisted scalar lands under its record key (``count_tokens``; ``provider``,
        ``usage_prompt_tokens``, ...), with ``<key>_note`` null on success and naming
        the reason otherwise -- a call the driver made is always visible as such,
        even when the value could not be read.
        """
        t0 = record.pop("_t0", None)
        elapsed = round((time.monotonic() - t0) * 1000, 3) if t0 is not None else None
        record["duration_ms"] = elapsed if completed else None
        record["duration_note"] = None if completed else (incomplete_note or "response did not complete")
        if entry == "chat_completions":
            # WO-8 needs positive evidence of a completed 2xx final response.
            # Status is transport metadata, never response content.
            record["http_status"] = status
        if entry:
            payload: dict | None = None
            if note is None:
                if status is not None and status >= 400:
                    note = f"upstream answered HTTP {status}"
                else:
                    payload, _decoded = _parse_json_object(response_body or b"", content_encoding)
                    if payload is None:
                        note = ("response body not parseable as a JSON object "
                                f"(encoding: {content_encoding or 'identity'})")
            for scalar in RESPONSE_SCALAR_ALLOWLIST[entry].scalars:
                value, why = (None, note) if payload is None else _extract_scalar(payload, scalar)
                record[scalar.key] = value
                record[f"{scalar.key}_note"] = why
        self._write(record)


def summarize(record_file: Path, disallowed: tuple[str, ...]) -> dict | None:
    """Digest a capture file into the meta-record shape; None when nothing captured.

    ``requests_recorded`` counts every line, unparseable (wire-basis) ones included;
    ``requests_readable`` counts the lines whose body parsed -- the ones on which a
    tool surface could have been read at all.
    """
    if not record_file.exists():
        return None
    records = read_records(record_file)
    # Pre-2026-09 capture files carry no basis field; every line in them parsed.
    readable = [r for r in records if r.get("body_bytes_basis", "decompressed") != "wire"]
    with_tools = [r for r in records if r.get("tool_names") is not None]
    union: list[str] = []
    for record in with_tools:
        for name in record["tool_names"]:
            if name not in union:
                union.append(name)
    return {
        "requests_recorded": len(records),
        "requests_readable": len(readable),
        "requests_with_tools": len(with_tools),
        "tool_names_union": union,
        "disallowed_builtins_on_wire": [n for n in disallowed if n in union],
        # How many allowlisted count-tokens calls the driver itself made (the scalar
        # lives on those lines as ``count_tokens``); zero when it never called one.
        "count_tokens_calls": sum(1 for r in records if "count_tokens" in r),
        # Of those, the calls whose scalar could not be read (null on the line, the
        # note says why -- e.g. an encoding the recorder cannot decode). A failed
        # read is a call that happened, never an absent call (DR-3).
        "count_tokens_unread": sum(1 for r in records if "count_tokens" in r and r["count_tokens"] is None),
    }


def read_records(record_file: Path, *, include_probe: bool = False) -> list[dict]:
    """The parsed lines of a capture file. Lines stamped ``probe`` (the loop driver's
    calibration probe) are never scored requests and are left out unless asked for."""
    records: list[dict] = []
    if not record_file.exists():
        return records
    for line in record_file.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("probe") and not include_probe:
            continue
        records.append(record)
    return records


def surface_mismatches(records: list[dict], expected: list[str]) -> list[dict]:
    """S7 in its exact form (50-drivers.md loop #3): every recorded tools array must
    EQUAL the expected wire surface. One entry per request that differs, naming the
    extra and missing names; a request with no tools array is missing all of them."""
    want = set(expected)
    out: list[dict] = []
    for record in records:
        names = record.get("tool_names")
        got = set(names) if isinstance(names, list) else set()
        if got != want:
            out.append({"seq": record.get("seq"),
                        "extra": sorted(got - want),
                        "missing": sorted(want - got)})
    return out


def loop_digest(record_file: Path) -> dict:
    """Per-invocation sums of the chat-completions scalars off the recorder's lines:
    served providers with counts (``(absent)`` when a line carries none), the usage
    sums, cost, and how many lines had an unreadable provider or cost (a failed read
    is counted, never folded into zero). Probe-stamped lines are included only when
    the file is a probe's own capture."""
    records = read_records(record_file, include_probe=True)
    lines = [r for r in records if "provider" in r or "usage_cost" in r]
    providers: dict[str, int] = {}
    finish_reasons: dict[str, int] = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0}
    unread = {"provider": 0, "cost": 0}
    for r in lines:
        served = r.get("provider") or "(absent)"
        providers[served] = providers.get(served, 0) + 1
        reason = r.get("finish_reason") or "(absent)"
        finish_reasons[reason] = finish_reasons.get(reason, 0) + 1
        if r.get("provider") is None:
            unread["provider"] += 1
        for key, field_name in (("usage_prompt_tokens", "prompt_tokens"),
                                ("usage_completion_tokens", "completion_tokens"),
                                ("usage_reasoning_tokens", "reasoning_tokens")):
            value = r.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[field_name] += value
        cost = r.get("usage_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            usage["cost_usd"] += cost
        else:
            unread["cost"] += 1
    usage["cost_usd"] = round(usage["cost_usd"], 8)
    ordered = sorted(lines, key=lambda r: r.get("seq") or 0)
    completed = [r for r in ordered if r.get("finish_reason")]
    return {"requests": len(lines), "served_providers": providers, "usage": usage,
            "finish_reasons": finish_reasons,
            # The last completed response's finish reason (by seq): the scored turn's
            # final one, since the scored turn is the invocation's last.
            "finish_reason_final": completed[-1]["finish_reason"] if completed else None,
            "provider_unread": unread["provider"], "cost_unread": unread["cost"], "lines": lines}
