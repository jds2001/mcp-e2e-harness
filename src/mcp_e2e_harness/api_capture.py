"""API-boundary tool-surface capture: ground truth off the wire, not a model claim.

Q2's preferred instrument (documentation/90-open-questions.md, ruling 455568a): the
driver's outbound API traffic is routed through this transparent recording proxy, and
the harness records the actual ``tools`` array sent to the model on every request.
That makes the builtins' absence an *observation* rather than an argv assertion, with
zero disclosure -- recording an outgoing request changes nothing in the consumer's
context -- so it runs during scored cells, continuously, unlike a calibration probe.
Nothing model-mediated sits in the verification path.

What is recorded, per request that carries a ``tools`` array (one JSONL line):
``seq``, ``at``, ``path``, ``model``, ``tool_names``. Nothing else. Never headers
(credentials ride there), never message content, never tool schemas. Requests without
a parseable ``tools`` array are forwarded untouched and counted, not recorded.

Forwarding is streaming (chunked pass-through), so SSE responses reach the driver as
they arrive; the proxy must never change the timing shape enough to alter driver
behavior. Forwarding never depends on parsing: an unparseable body is forwarded
verbatim.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Hop-by-hop headers; everything else is forwarded verbatim in both directions.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


class ApiSurfaceRecorder:
    """A localhost HTTP server that records tool surfaces and forwards to ``upstream``."""

    def __init__(self, record_file: Path, upstream: str):
        self.record_file = record_file
        self.upstream = urlsplit(upstream)
        if self.upstream.scheme not in ("http", "https") or not self.upstream.netloc:
            raise ValueError(f"upstream must be an http(s) URL, got {upstream!r}")
        self._lock = threading.Lock()
        self._seq = 0
        self.unrecorded_requests = 0
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

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---------------------------------------------------------------- recording

    def _record(self, path: str, body: bytes) -> None:
        try:
            payload = json.loads(body)
            tools = payload.get("tools")
            if not isinstance(tools, list):
                raise ValueError
            # name-or-type: hosted tools in the Responses API (web_search and kin)
            # carry a "type" and no "name"; recording only names would blind the
            # disallowed-builtins check to exactly the tools it exists to catch.
            names = [t.get("name") or t.get("type") for t in tools if isinstance(t, dict)]
            names = [n for n in names if n]
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, ValueError):
            with self._lock:
                self.unrecorded_requests += 1
            return
        with self._lock:
            self._seq += 1
            line = json.dumps({
                "seq": self._seq,
                "at": datetime.now(timezone.utc).isoformat(),
                "path": path.split("?")[0],
                "model": payload.get("model"),
                "tool_names": names,
            })
            with open(self.record_file, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
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
        if body:
            self._record(handler.path, body)

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
            return
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
                handler.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass  # driver hung up mid-stream; nothing to salvage
        finally:
            conn.close()


def summarize(record_file: Path, disallowed: tuple[str, ...]) -> dict | None:
    """Digest a capture file into the meta-record shape; None when nothing captured."""
    if not record_file.exists():
        return None
    records = []
    for line in record_file.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    union: list[str] = []
    for record in records:
        for name in record.get("tool_names") or []:
            if name not in union:
                union.append(name)
    return {
        "requests_with_tools": len(records),
        "tool_names_union": union,
        "disallowed_builtins_on_wire": [n for n in disallowed if n in union],
    }
