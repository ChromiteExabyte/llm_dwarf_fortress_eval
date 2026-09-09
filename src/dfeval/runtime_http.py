"""Authenticated loopback development transport; this is not an OS sandbox.

No files, game controls or process execution are exposed. The broker records a
provider response before delivery; this transport cannot prove client receipt.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import socket
import socketserver
import threading
import time
from http import HTTPStatus
from typing import Any

from .policies import strict_json
from .runtime import RuntimeBroker, RuntimeRejected
from .runtime_witness import WitnessError

HEADER_LIMIT = 16384
HEADER_COUNT = 32
READ_SECONDS = 5.0
WRITE_SECONDS = 2.0
_FIELD = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_REQUEST = re.compile(rb"([A-Z]+) ([^\x00-\x20\x7f]+) HTTP/1\.[01]")


class _HTTPError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def _rejection_status(code: str) -> int:
    if code == "invalid_request":
        return 400
    if code in {"busy", "closed", "abandoned"}:
        return 409
    if code.endswith("_limit"):
        return 429
    if code == "timeout":
        return 504
    return 502


class _RuntimeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, broker: RuntimeBroker, port: int, token: str):
        self.broker, self.token = broker, token
        self.connection_limit = broker.limits.max_in_flight + 4
        self._slots = threading.BoundedSemaphore(self.connection_limit)
        super().__init__(("127.0.0.1", port), _Handler)
        self.timeout = 0.25  # also usable by the CLI's bounded handle_request loop

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Never print untrusted request data, credentials or exception details.
        pass


class _Handler(socketserver.BaseRequestHandler):
    server: _RuntimeServer
    request: socket.socket

    def _recv(self, count: int, deadline: float) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _HTTPError(408, "request_timeout")
        self.request.settimeout(remaining)
        try:
            data = self.request.recv(count)
        except TimeoutError:
            raise _HTTPError(408, "request_timeout") from None
        if not data:
            raise _HTTPError(400, "incomplete_request")
        if time.monotonic() > deadline:
            raise _HTTPError(408, "request_timeout")
        return data

    def _read(self) -> tuple[str, bytes, dict[str, list[str]], bytes]:
        deadline = time.monotonic() + READ_SECONDS
        received = b""
        while b"\r\n\r\n" not in received:
            if len(received) >= HEADER_LIMIT:
                raise _HTTPError(431, "headers_too_large")
            received += self._recv(min(4096, HEADER_LIMIT - len(received)), deadline)
        raw_headers, body = received.split(b"\r\n\r\n", 1)
        lines = raw_headers.split(b"\r\n")
        request = _REQUEST.fullmatch(lines[0]) if len(lines[0]) <= 2048 else None
        if request is None or len(lines) - 1 > HEADER_COUNT:
            raise _HTTPError(400, "invalid_headers")
        method, path = request.groups()
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if not separator or not _FIELD.fullmatch(name) or any(
                    byte < 32 and byte != 9 or byte == 127 for byte in value):
                raise _HTTPError(400, "invalid_headers")
            headers.setdefault(name.decode("ascii").lower(), []).append(value.strip(b" \t").decode("latin1"))
        expected_host = f"127.0.0.1:{self.server.server_address[1]}"
        if headers.get("host") != [expected_host]:
            raise _HTTPError(400, "invalid_host")
        authorization = headers.get("authorization", [])
        expected_auth = ("Bearer " + self.server.token).encode("ascii")
        if len(authorization) != 1 or not hmac.compare_digest(authorization[0].encode("latin1"), expected_auth):
            raise _HTTPError(401, "unauthorized")
        if "origin" in headers:
            raise _HTTPError(403, "browser_origin_rejected")
        if any(field in headers for field in ("transfer-encoding", "content-encoding", "expect")):
            raise _HTTPError(400, "unsupported_framing")
        lengths = headers.get("content-length", [])
        if len(lengths) > 1 or lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise _HTTPError(400, "invalid_content_length")
        if method == b"POST" and len(lengths) != 1:
            raise _HTTPError(411, "content_length_required")
        length = int(lengths[0]) if lengths else 0
        if length > self.server.broker.limits.max_request_bytes:
            raise _HTTPError(413, "request_too_large")
        if method == b"GET" and length or len(body) > length:
            raise _HTTPError(400, "unexpected_body")
        if (method, path) not in {(b"GET", b"/v1/runtime"), (b"POST", b"/v1/infer")}:
            raise _HTTPError(404, "unknown_route")
        if method == b"POST" and headers.get("content-type") not in (
                ["application/json"], ["application/json; charset=utf-8"]):
            raise _HTTPError(415, "json_required")
        while len(body) < length:
            body += self._recv(min(65536, length - len(body)), deadline)
        return method.decode("ascii"), path, headers, body

    def _send(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        headers = (f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
                   "Content-Type: application/json; charset=utf-8\r\n"
                   f"Content-Length: {len(body)}\r\n"
                   "Connection: close\r\nCache-Control: no-store\r\n"
                   "X-Content-Type-Options: nosniff\r\n\r\n").encode("ascii")
        self.request.settimeout(WRITE_SECONDS)
        self.request.sendall(headers + body)

    def handle(self) -> None:
        try:
            method, _, _, body = self._read()
            if method == "GET":
                response = self.server.broker.description()
            else:
                try:
                    value = strict_json(body.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    raise _HTTPError(400, "invalid_json") from None
                response = self.server.broker.infer(value)
            status = 200
        except _HTTPError as exc:
            status, response = exc.status, {"error": exc.code}
        except RuntimeRejected as exc:
            status, response = _rejection_status(exc.code), {"error": exc.code}
        except WitnessError:
            status, response = 503, {"error": "witness_unavailable"}
        except OSError:
            return  # disconnected input; any dispatched broker call owns its evidence
        except Exception:
            status, response = 500, {"error": "internal_error"}
        try:
            self._send(status, response)
        except (OSError, ValueError, UnicodeError):
            pass  # a recorded response is not proof of successful client delivery


def make_server(broker: RuntimeBroker, *, port: int = 0, token: str | None = None) -> _RuntimeServer:
    """Create a loopback server. Closing it never closes the broker it borrows."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer in 0..65535")
    token = secrets.token_urlsafe(32) if token is None else token
    if type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("token must contain 32..256 URL-safe characters")
    return _RuntimeServer(broker, port, token)
