"""Exercise only a fake broker on loopback; no model or game is contacted."""

import json
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from dfeval import runtime_http
from dfeval.runtime import RuntimeRejected
from dfeval.runtime_witness import WitnessError


class FakeBroker:
    def __init__(self):
        self.limits = SimpleNamespace(max_in_flight=1, max_request_bytes=4096)
        self.calls, self.records = [], []
        self.error = None
        self.entered, self.finished = threading.Event(), threading.Event()
        self.release = None
        self.closed = False

    def description(self):
        return {"isolation": "none", "game_connected": False, "executes_code": False}

    def infer(self, value):
        self.calls.append(value)
        if self.error:
            raise self.error
        if not isinstance(value, dict):
            raise RuntimeRejected("invalid_request", "private details")
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(2)
        result = {"call_id": len(self.calls), "text": "recorded output"}
        self.records.append(result)
        self.finished.set()
        return result

    def close(self):
        self.closed = True


@contextmanager
def running(broker=None):
    broker = broker or FakeBroker()
    server = runtime_http.make_server(broker)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield broker, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def packet(server, *, method="POST", path="/v1/infer", body=b"{}", remove=(), extra=()):
    headers = [("Host", f"127.0.0.1:{server.server_address[1]}"),
               ("Authorization", "Bearer " + server.token),
               ("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    headers = [(key, value) for key, value in headers if key.lower() not in remove]
    headers.extend(extra)
    return (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers)
            + "\r\n").encode("ascii") + body


def read_all(connection):
    result = b""
    while True:
        try:
            data = connection.recv(65536)
        except ConnectionResetError:
            break
        if not data:
            break
        result += data
    return result


def exchange(server, request):
    with socket.create_connection(server.server_address, timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        raw = read_all(connection)
    headers, body = raw.split(b"\r\n\r\n", 1)
    return int(headers.split(b" ", 2)[1]), json.loads(body), headers


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_description_and_independent_text_contexts_have_no_transport_additions():
    with running() as (broker, server):
        assert server.server_address[0] == "127.0.0.1" and len(server.token) >= 32
        status, description, headers = exchange(server, packet(server, method="GET", path="/v1/runtime", body=b""))
        assert status == 200 and description == broker.description()
        assert b"Connection: close" in headers and b"Access-Control-Allow-Origin" not in headers
        context = {"messages": [{"role": "system", "content": "Independent instructions 矮人"},
                                {"role": "user", "content": "A caller's own context"}], "max_output_tokens": 8}
        status, result, _ = exchange(server, packet(server, body=json.dumps(context, ensure_ascii=False).encode("utf-8")))
        assert status == 200 and result == broker.records[0] and broker.calls == [context]
        assert server.token not in json.dumps(description) + json.dumps(result)
    assert broker.closed is False  # transport borrows the supervisor


@pytest.mark.parametrize("remove,extra,status", [
    (("authorization",), (), 401), (("authorization",), (("Authorization", "Bearer wrong"),), 401),
    ((), (("Authorization", "Bearer duplicate"),), 401),
    (("host",), (), 400), (("host",), (("Host", "example.com:80"),), 400),
    ((), (("Host", "127.0.0.1:1"),), 400), ((), (("Origin", "null"),), 403),
    ((), (("Transfer-Encoding", "chunked"),), 400), ((), (("Content-Encoding", "gzip"),), 400),
    ((), (("Expect", "100-continue"),), 400), ((), (("Content-Length", "2"),), 400),
    (("content-length",), (("Content-Length", "-1"),), 400),
    (("content-length",), (("Content-Length", "2, 2"),), 400), (("content-length",), (), 411),
    (("content-type",), (("Content-Type", "text/plain"),), 415),
])
def test_authentication_origin_and_framing_fail_before_inference(remove, extra, status):
    with running() as (broker, server):
        response = exchange(server, packet(server, remove=remove, extra=extra))
        assert response[0] == status and broker.calls == []
        assert server.token not in json.dumps(response[1])


@pytest.mark.parametrize("body", [b'{"x": 1, "x": 2}', b'{"x": NaN}', b'{"x": Infinity}', b'"\xff"', b'{} trailing'])
def test_invalid_json_is_never_passed_to_the_broker(body):
    with running() as (broker, server):
        assert exchange(server, packet(server, body=body))[0] == 400
        assert broker.calls == []


@pytest.mark.parametrize("method,path", [("GET", "/v1/infer"), ("POST", "/v1/infer?x=1"),
                                         ("POST", "http://127.0.0.1/v1/infer"), ("DELETE", "/v1/runtime"),
                                         ("POST", "/exec"), ("GET", "/workspace/file")])
def test_routes_do_not_expand_into_files_or_execution(method, path):
    with running() as (broker, server):
        assert exchange(server, packet(server, method=method, path=path, body=b""))[0] == 404
        assert broker.calls == []


def test_raw_body_header_bounds_and_incomplete_or_pipelined_requests():
    with running() as (broker, server):
        oversized = packet(server, body=b"", remove=("content-length",), extra=(("Content-Length", "4097"),))
        assert exchange(server, oversized)[0] == 413
        oversized_headers = packet(server, extra=(("X-Padding", "a" * runtime_http.HEADER_LIMIT),))
        assert exchange(server, oversized_headers)[0] == 431
        too_many = packet(server, extra=tuple((f"X-{index}", "a") for index in range(runtime_http.HEADER_COUNT)))
        assert exchange(server, too_many)[0] == 400
        incomplete = packet(server, remove=("content-length",), extra=(("Content-Length", "3"),))
        assert exchange(server, incomplete)[0] == 400
        assert exchange(server, packet(server) + packet(server))[0] == 400
        assert broker.calls == []


@pytest.mark.parametrize("body_stage", [False, True])
def test_absolute_upload_deadline_expires_even_while_bytes_keep_arriving(monkeypatch, body_stage):
    monkeypatch.setattr(runtime_http, "READ_SECONDS", 0.15)
    with running() as (broker, server):
        with socket.create_connection(server.server_address, timeout=1) as connection:
            if body_stage:
                connection.sendall(packet(server, body=b"", remove=("content-length",), extra=(("Content-Length", "100"),)))
            else:
                connection.sendall(b"POST ")
            stopped = threading.Event()
            def trickle():
                while not stopped.wait(0.025):
                    try:
                        connection.sendall(b"x")
                    except OSError:
                        break
            thread = threading.Thread(target=trickle, daemon=True)
            thread.start()
            started = time.monotonic()
            try:
                response = read_all(connection)
            finally:
                stopped.set()
                thread.join(timeout=1)
            assert response.startswith(b"HTTP/1.1 408 ") and time.monotonic() - started < 0.8
        assert broker.calls == []


def test_connection_cap_applies_before_headers_are_uploaded():
    with running() as (broker, server):
        connections = [socket.create_connection(server.server_address, timeout=2) for _ in range(server.connection_limit)]
        try:
            wait_for(lambda: server._slots._value == 0)
            with socket.create_connection(server.server_address, timeout=1) as overflow:
                assert read_all(overflow) == b""
            connections[0].close()
            wait_for(lambda: server._slots._value == 1)
            assert exchange(server, packet(server))[0] == 200
        finally:
            for connection in connections:
                connection.close()


def test_disconnected_client_does_not_cancel_the_dispatched_broker_call():
    broker = FakeBroker()
    broker.release = threading.Event()
    with running(broker) as (_, server):
        connection = socket.create_connection(server.server_address, timeout=2)
        connection.sendall(packet(server))
        assert broker.entered.wait(1)
        connection.close()
        broker.release.set()
        assert broker.finished.wait(1)
        wait_for(lambda: server._slots._value == server.connection_limit)
        assert len(broker.calls) == len(broker.records) == 1


@pytest.mark.parametrize("error,status,code", [
    (RuntimeRejected("invalid_request", "private details"), 400, "invalid_request"),
    (RuntimeRejected("busy", "private details"), 409, "busy"),
    (RuntimeRejected("closed", "private details"), 409, "closed"),
    (RuntimeRejected("resource_limit", "private details"), 429, "resource_limit"),
    (RuntimeRejected("provider_error", "private details"), 502, "provider_error"),
    (RuntimeRejected("timeout", "private details"), 504, "timeout"),
    (WitnessError("private path and credential"), 503, "witness_unavailable"),
])
def test_supervisor_errors_expose_only_short_codes_and_never_logs(error, status, code, capsys):
    broker = FakeBroker()
    broker.error = error
    with running(broker) as (_, server):
        actual, result, _ = exchange(server, packet(server))
        assert actual == status and result == {"error": code}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("kwargs", [{"port": True}, {"port": -1}, {"port": 65536},
                                     {"token": "short"}, {"token": "x" * 32 + "\r\n"}, {"token": "矮" * 32}])
def test_invalid_listener_configuration_is_rejected_before_binding(kwargs):
    with pytest.raises(ValueError):
        runtime_http.make_server(FakeBroker(), **kwargs)
