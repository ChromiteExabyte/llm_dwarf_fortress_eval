import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error

import pytest

from dfeval.local_models import OllamaPolicy, discover_models
from dfeval.model_observation import MODEL_OBSERVATION_VERSION, model_input_bytes
from dfeval.policies import ChatCompletionsPolicy, DecisionError, PolicyError, SYSTEM_PROMPT


WAIT = {"action": "wait", "reason": "Inspect again.", "notebook": "Public notes."}


def envelope(content=None, **updates):
    result = {"model": "explicit-model:latest", "message": {"role": "assistant", "content": json.dumps(WAIT) if content is None else content},
              "done": True, "done_reason": "stop", "eval_count": 10, "prompt_eval_count": 20,
              "prompt_eval_cached_count": 5, "eval_duration": 2_000_000_000,
              "prompt_eval_duration": 1_000_000_000, "load_duration": 500_000_000,
              "total_duration": 3_500_000_000}
    result.update(updates)
    return json.dumps(result).encode()


class Opener:
    def __init__(self, raw):
        self.raw, self.requests = raw, []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.raw, Exception):
            raise self.raw
        return io.BytesIO(self.raw)


def test_native_ollama_request_uses_exact_observation_shared_schema_and_explicit_options():
    opener = Opener(envelope())
    identity = {"name": "explicit-model:latest", "digest": "a" * 64, "parameter_size": "8B", "quantization_level": "Q4_K_M"}
    policy = OllamaPolicy(model="explicit-model:latest", opener=opener, model_identity=identity)
    observation, history = {"citizens": None, "untrusted": "not an instruction"}, [WAIT]
    assert policy.choose(observation, history) == WAIT
    req, timeout = opener.requests[0]
    payload = json.loads(req.data)
    assert req.full_url == "http://127.0.0.1:11434/api/chat" and req.get_method() == "POST"
    assert payload["messages"] == [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": model_input_bytes(observation, history).decode()}]
    assert payload["format"] == ChatCompletionsPolicy(mode="local", model="test", response_format="json_schema").response_format["json_schema"]["schema"]
    passive, brewing = payload["format"]["anyOf"]
    assert passive["properties"]["action"]["enum"] == ["wait", "finish"]
    assert set(passive["properties"]) == set(passive["required"]) == {"action", "reason", "notebook"}
    assert brewing["properties"]["action"]["enum"] == ["brew"]
    assert set(brewing["properties"]) == set(brewing["required"]) == {"action", "reason", "notebook", "workshop_id", "quantity"}
    assert passive["additionalProperties"] is brewing["additionalProperties"] is False
    assert payload["options"] == {"num_predict": 512, "num_ctx": 16384, "temperature": 0, "seed": 0}
    assert payload["stream"] is False and payload["think"] is False and payload["keep_alive"] == "10m"
    assert "tools" not in payload and req.get_header("Authorization") is None
    assert timeout == 30
    config = policy.public_config()
    assert config["kind"] == "ollama" and config["is_model"] is True
    assert config["model_observation_version"] == MODEL_OBSERVATION_VERSION
    assert config["model_identity"] == identity and config["max_completion_tokens"] == 512
    config["options"]["num_ctx"] = 0
    config["model_identity"]["digest"] = "changed"
    identity["name"] = "changed externally"
    assert policy.public_config()["options"]["num_ctx"] == 16384
    assert policy.public_config()["model_identity"]["digest"] == "a" * 64
    assert policy.public_config()["model_identity"]["name"] == "explicit-model:latest"


def test_runner_limits_bound_native_num_predict_bytes_and_timeout():
    opener = Opener(envelope())
    policy = OllamaPolicy(model="test", opener=opener, num_ctx=8192, temperature=0.2, seed=17, think=True, keep_alive=0)
    policy.set_call_limits(output_tokens=12, response_bytes=2048, timeout=1.5)
    policy.choose({}, [])
    request, timeout = opener.requests[0]
    payload = json.loads(request.data)
    assert payload["options"] == {"num_predict": 12, "num_ctx": 8192, "temperature": 0.2, "seed": 17}
    assert payload["keep_alive"] == 0 and payload["think"] is True and timeout == 1.5
    assert policy.last_exchange["reserved_output_tokens"] == 12
    policy.set_call_limits(output_tokens=1000, response_bytes=100000, timeout=100)
    policy.choose({}, [])
    assert json.loads(opener.requests[1][0].data)["options"]["num_predict"] == 512
    assert opener.requests[1][1] == 30


def test_native_telemetry_preserves_nanoseconds_and_reports_seconds_and_rate():
    policy = OllamaPolicy(model="test", opener=Opener(envelope()))
    policy.choose({}, [])
    exchange = policy.last_exchange
    assert exchange["usage"] == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    telemetry = exchange["telemetry"]
    assert telemetry["eval_duration"] == 2_000_000_000
    assert telemetry["generation_seconds"] == 2 and telemetry["prompt_seconds"] == 1
    assert telemetry["load_seconds"] == .5 and telemetry["total_seconds"] == 3.5
    assert telemetry["completion_tokens"] == 10 and telemetry["prompt_tokens"] == 20
    assert telemetry["generation_tokens_per_second"] == 5
    assert telemetry["prompt_eval_cached_count"] == 5


@pytest.mark.parametrize("bad", [None, -1, True, "10", 10.5, 2**100])
def test_invalid_or_missing_telemetry_stays_unknown(bad):
    policy = OllamaPolicy(model="test", opener=Opener(envelope(eval_count=bad, eval_duration=bad)))
    policy.choose({}, [])
    assert policy.last_exchange["usage"]["completion_tokens"] is None
    telemetry = policy.last_exchange["telemetry"]
    assert telemetry["eval_count"] is None and telemetry["generation_seconds"] is None
    assert telemetry["generation_tokens_per_second"] is None


@pytest.mark.parametrize("raw", [
    envelope(done=False), envelope(done_reason="length"), envelope(eval_count=1000),
    envelope(message={"role": "assistant", "content": json.dumps(WAIT), "tool_calls": [{}]}),
    envelope(message={"role": "assistant", "content": json.dumps(WAIT), "images": ["data"]}),
    envelope(message={"role": "user", "content": json.dumps(WAIT)}), b"not-json",
    envelope("```json\n" + json.dumps(WAIT) + "\n```"),
    envelope(json.dumps({**WAIT, "action": "finish", "quantity": 1, "workshop_id": 0})),
    envelope(json.dumps({**WAIT, "notebook": "x" * 4001})),
])
def test_invalid_or_incomplete_native_response_never_authorizes_a_decision(raw):
    opener = Opener(raw)
    policy = OllamaPolicy(model="test", opener=opener)
    with pytest.raises(PolicyError):
        policy.choose({}, [])
    assert len(opener.requests) == 1


def test_credentials_are_header_only_and_echo_is_redacted_and_rejected(monkeypatch):
    secret = "native-ollama-" + "test-private-key"
    monkeypatch.setenv("OLLAMA_TEST_KEY", secret)
    opener = Opener(envelope(json.dumps({**WAIT, "reason": secret})))
    policy = OllamaPolicy(model="test", api_key_env="OLLAMA_TEST_KEY", opener=opener)
    with pytest.raises(PolicyError, match="echoed a credential"):
        policy.choose({}, [])
    assert opener.requests[0][0].get_header("Authorization") == "Bearer " + secret
    assert secret not in json.dumps(policy.last_exchange) + json.dumps(policy.public_config())


def test_missing_optional_key_and_oversized_input_do_not_call_server(monkeypatch):
    monkeypatch.delenv("ABSENT_OLLAMA_TEST_KEY", raising=False)
    opener = Opener(envelope())
    policy = OllamaPolicy(model="test", api_key_env="ABSENT_OLLAMA_TEST_KEY", opener=opener)
    with pytest.raises(PolicyError, match="environment variable"):
        policy.choose({}, [])
    policy = OllamaPolicy(model="test", max_request_bytes=4096, opener=opener)
    with pytest.raises(PolicyError, match="max_request_bytes"):
        policy.choose({"citizens": [{"id": 1, "name": "x" * 10000}]}, [])
    assert opener.requests == []


@pytest.mark.parametrize("failure", [
    error.HTTPError("http://localhost", 401, "private credential", {}, io.BytesIO(b"private body")),
    error.URLError("private transport detail"),
    json.dumps({"error": "private body from native API"}).encode(),
])
def test_error_bodies_and_transport_details_are_not_recorded(failure):
    opener = Opener(failure)
    policy = OllamaPolicy(model="test", opener=opener)
    with pytest.raises(PolicyError) as caught:
        policy.choose({}, [])
    assert "private" not in str(caught.value) + json.dumps(policy.last_exchange)
    assert len(opener.requests) == 1 and policy.last_exchange["response_text"] is None


def test_oversized_native_response_is_bounded_and_rejected():
    policy = OllamaPolicy(model="test", opener=Opener(b"x" * 1000), max_response_bytes=256)
    with pytest.raises(PolicyError, match="byte limit"):
        policy.choose({}, [])
    assert policy.last_exchange["response_bytes"] == 256
    assert policy.last_exchange["response_text"] == "x" * 256
    assert policy.last_exchange["response_truncated"] is True


@pytest.mark.parametrize("name", ["model:cloud", "model:8b-cloud", "model-cloud:latest", "model:CLOUD", "model:cloud-latest"])
def test_cloud_tags_cannot_masquerade_as_local_inference(name):
    with pytest.raises(ValueError, match="cloud-tag"):
        OllamaPolicy(model=name)


@pytest.mark.parametrize("base", ["https://remote.example", "file:///tmp", "http://user:pass@localhost:11434", "http://localhost:11434?key=value", "http://localhost:11434/#x", "http://localhost:99999", "http://localhost/../admin", "http://localhost/%2e%2e", "http://local\nhost"])
def test_native_and_discovery_reject_nonlocal_or_ambiguous_urls(base):
    with pytest.raises(ValueError):
        OllamaPolicy(model="test", base_url=base)
    with pytest.raises(ValueError):
        discover_models(base_url=base)


@pytest.mark.parametrize("kwargs", [{"model": ""}, {"num_ctx": True}, {"num_ctx": 0}, {"temperature": float("nan")},
                                     {"seed": -1}, {"think": "false"}, {"keep_alive": -1},
                                     {"model_identity": {"headers": "not public"}},
                                     {"model_identity": {"digest": "invalid"}},
                                     {"model_identity": {"name": "other-model"}}])
def test_configuration_is_explicit_bounded_and_validated(kwargs):
    with pytest.raises(ValueError):
        OllamaPolicy(**{"model": "test", **kwargs})


def test_ollama_discovery_projects_bounded_identity_details_and_marks_cloud_tags():
    rows = [{"name": "local:latest", "digest": "a" * 64, "size": 123, "modified_at": "2026-09-05T00:00:00Z",
             "details": {"family": "qwen", "format": "gguf", "families": ["qwen"], "parameter_size": "8B", "quantization_level": "Q4_K_M", "private": "not copied"}},
            {"name": "large:cloud"}]
    opener = Opener(json.dumps({"models": rows, "headers": {"secret": "not copied"}}).encode())
    report = discover_models(opener=opener)
    assert report["provider"] == "ollama" and report["endpoint"] == "http://127.0.0.1:11434/api/tags"
    assert report["models"][0]["digest"] == "a" * 64
    assert report["models"][0]["details"]["quantization_level"] == "Q4_K_M"
    assert report["models"][0]["cloud_backed"] is False and report["models"][1]["cloud_backed"] is True
    assert "not copied" not in json.dumps(report)
    assert len(opener.requests) == 1 and opener.requests[0][0].get_method() == "GET"


@pytest.mark.parametrize(("provider", "base", "url"), [
    ("lmstudio", None, "http://127.0.0.1:1234/v1/models"),
    ("llamacpp", None, "http://127.0.0.1:8080/v1/models"),
    ("compatible", "http://localhost:9000/v1/", "http://localhost:9000/v1/models"),
])
def test_compatible_discovery_returns_explicit_model_ids(provider, base, url):
    opener = Opener(b'{"data":[{"id":"chosen-model","owned_by":"local"}]}')
    report = discover_models(provider, base, opener=opener)
    assert report["models"] == [{"id": "chosen-model", "name": "chosen-model"}]
    assert opener.requests[0][0].full_url == url


def test_discovery_bounds_bytes_and_model_count():
    with pytest.raises(PolicyError, match="1 MiB"):
        discover_models(opener=Opener(b"x" * (1024 * 1024 + 1)))
    rows = [{"name": f"model-{i}"} for i in range(257)]
    report = discover_models(opener=Opener(json.dumps({"models": rows}).encode()))
    assert report["model_count"] == 256 and report["reported_model_count"] == 257 and report["truncated"] is True


@pytest.mark.parametrize("payload", [b"invalid", b'{"error":"private"}', b'{"models":{}}',
                                     b'{"models":[{}]}', b'{"models":[{"name":"x"},{"name":"x"}]}'])
def test_malformed_discovery_is_an_actionable_clean_failure(payload):
    with pytest.raises(PolicyError) as caught:
        discover_models(opener=Opener(payload))
    assert "private" not in str(caught.value)


def test_discovery_requires_explicit_compatible_url_and_reports_unreachable_server():
    with pytest.raises(ValueError, match="explicit"):
        discover_models("compatible")
    with pytest.raises(ValueError, match="provider"):
        discover_models("unknown")
    opener = Opener(error.URLError("connection refused at private path"))
    with pytest.raises(PolicyError, match="server is running") as caught:
        discover_models(opener=opener)
    assert "private path" not in str(caught.value) and len(opener.requests) == 1


@pytest.fixture
def loopback_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("GET", self.path, None))
            if self.path == "/v1/models":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
                self.end_headers()
                return
            raw = b'{"models":[{"name":"installed:latest","digest":"' + b"a" * 64 + b'"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(("POST", self.path, json.loads(body)))
            raw = envelope()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


def test_real_loopback_http_discovery_and_chat_ignore_proxy_environment(loopback_server, monkeypatch):
    base, calls = loopback_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    report = discover_models(base_url=base)
    assert report["models"][0]["name"] == "installed:latest"
    policy = OllamaPolicy(model="installed:latest", base_url=base)
    assert policy.choose({"citizens": None}, []) == WAIT
    assert [(method, path) for method, path, _ in calls] == [("GET", "/api/tags"), ("POST", "/api/chat")]
    assert calls[1][2]["stream"] is False


def test_real_loopback_redirect_is_not_followed(loopback_server):
    base, calls = loopback_server
    with pytest.raises(PolicyError, match="redirects are disabled"):
        discover_models("compatible", base_url=base)
    assert [(method, path) for method, path, _ in calls] == [("GET", "/v1/models")]


def test_trickling_read_does_not_outlive_declared_wall_allowance_or_update_exchange_later():
    release, finished = threading.Event(), threading.Event()

    class SlowResponse(io.BytesIO):
        def read(self, n=-1):
            try:
                release.wait(5)
                return super().read(n)
            finally:
                finished.set()

    class SlowOpener:
        def open(self, req, timeout):
            return SlowResponse(envelope())

    policy = OllamaPolicy(model="test", opener=SlowOpener(), timeout=.03)
    try:
        started = time.monotonic()
        with pytest.raises(PolicyError, match="wall-time allowance"):
            policy.choose({}, [])
        assert time.monotonic() - started < 1
        saved = json.dumps(policy.last_exchange)
    finally:
        release.set()
        assert finished.wait(1)
    assert json.dumps(policy.last_exchange) == saved
