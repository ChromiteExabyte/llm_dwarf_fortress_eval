from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
from urllib import error

import pytest

from dfeval.inference import TextClient, TextInferenceError, TextRequest


def response(provider="ollama", text="Ordinary unconstrained text", finish="stop", **fields):
    message = {"role": "assistant", "content": text}
    if provider == "ollama":
        body = {"message": message, "done": True, "done_reason": finish,
                "prompt_eval_count": 20, "eval_count": 10, "prompt_eval_cached_count": 5,
                "eval_duration": 2_000_000_000, "prompt_eval_duration": 1_000_000_000,
                "load_duration": 500_000_000, "total_duration": 3_500_000_000}
    else:
        body = {"choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                          "prompt_tokens_details": {"cached_tokens": 5}},
                "timings": {"predicted_ms": 2000, "prompt_ms": 1000}}
    body.update(fields)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class Opener:
    def __init__(self, raw=None):
        self.raw = response() if raw is None else raw
        self.requests = []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.raw, Exception):
            raise self.raw
        return io.BytesIO(self.raw)


def ask(text="Question", *, tokens=64, temperature=None):
    return TextRequest([{"role": "user", "content": text}], tokens, temperature)


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_independent_context_is_sent_exactly_without_an_injected_game_contract(provider):
    opener = Opener(response(provider))
    client = TextClient(provider=provider, model="operator-model", opener=opener)
    messages = [{"role": "system", "content": "A caller-defined objective."},
                {"role": "user", "content": "分析 🪨"},
                {"role": "assistant", "content": "A caller-selected prior reply."},
                {"role": "user", "content": "Continue in plain text."}]
    request = TextRequest(messages, 64, 0.25)
    result = client.infer(request)
    sent, timeout = opener.requests[0]
    body = json.loads(sent.data)
    assert body["messages"] == messages
    assert body["model"] == "operator-model" and body["stream"] is False
    assert set(body) == ({"model", "messages", "stream", "options"} if provider == "ollama"
                         else {"model", "messages", "stream", "temperature", "max_tokens"})
    if provider == "ollama":
        assert body["options"] == {"num_predict": 64, "temperature": 0.25}
    assert timeout == 30
    assert result["text"] == "Ordinary unconstrained text"
    assert result["finish_reason"] == "stop" and result["truncated"] is False
    exchange = result["exchange"]
    assert exchange["request"] == body
    assert exchange["request_text"].encode() == sent.data
    assert exchange["request_bytes"] == len(sent.data)
    assert exchange["response_text"].encode() == opener.raw
    assert exchange["response_bytes"] == len(opener.raw)
    assert exchange["response_complete"] is True
    assert exchange["provider_may_be_running"] is False
    assert not hasattr(client, "last_exchange")
    json.dumps(result, allow_nan=False)


def test_request_snapshots_nested_messages_and_returned_copies_are_independent():
    messages = [{"role": "user", "content": "Original"}]
    request = TextRequest(messages, 64)
    messages[0]["content"] = "Changed externally"
    messages.append({"role": "assistant", "content": "Extra"})
    request.messages[0]["content"] = "Changed through property"
    copy = request.to_dict()
    copy["messages"][0]["content"] = "Changed through export"
    assert request.to_dict() == {"messages": [{"role": "user", "content": "Original"}],
                                 "max_output_tokens": 64, "temperature": None}
    with pytest.raises(FrozenInstanceError):
        request.max_output_tokens = 99


def test_message_container_subclasses_cannot_change_values_between_validation_and_capture():
    class ChangingDict(dict):
        def __getitem__(self, key):
            raise AssertionError("Subclass lookup must not run")

    class ChangingList(list):
        def __iter__(self):
            raise AssertionError("Subclass iteration must not run")

    with pytest.raises(ValueError):
        TextRequest([ChangingDict(role="user", content="x")], 64)
    with pytest.raises(ValueError):
        TextRequest(ChangingList([{"role": "user", "content": "x"}]), 64)


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_no_server_side_conversation_identifier_or_prior_call_is_inserted(provider):
    opener = Opener(response(provider))
    client = TextClient(provider=provider, model="fixed", opener=opener)
    first = client.infer(ask("First context"))
    first["exchange"]["request"]["messages"][0]["content"] = "Mutated result"
    client.infer(ask("Independent context"))
    assert json.loads(opener.requests[-1][0].data)["messages"] == [{"role": "user", "content": "Independent context"}]


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_length_finish_retains_partial_text_and_usage(provider):
    client = TextClient(provider=provider, model="fixed", opener=Opener(response(provider, text="unfinished", finish="length")))
    result = client.infer(ask())
    assert result["text"] == "unfinished"
    assert result["finish_reason"] == result["exchange"]["finish_reason"] == "length"
    assert result["truncated"] is True
    assert result["exchange"]["response_truncated"] is False
    assert result["usage"]["completion_tokens"] == 10


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_reported_prompt_completion_cache_and_timings_are_preserved(provider):
    result = TextClient(provider=provider, model="fixed", opener=Opener(response(provider))).infer(ask())
    usage, timing = result["usage"], result["timing"]
    assert usage == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
                     "cached_prompt_tokens": 5,
                     "total_tokens_source": "sum_of_reported_counts" if provider == "ollama" else "provider_reported"}
    assert timing["prompt_seconds"] == 1 and timing["generation_seconds"] == 2
    assert timing["wall_seconds"] >= 0
    assert timing["load_seconds"] == (0.5 if provider == "ollama" else None)
    if provider == "ollama":
        assert timing["native"]["eval_duration"] == 2_000_000_000
    usage["prompt_tokens"] = 999
    assert result["exchange"]["usage"]["prompt_tokens"] == 20


@pytest.mark.parametrize("bad", [None, True, -1, 2**100, "10", 1.5])
@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_missing_or_invalid_usage_is_unknown_and_never_estimated(bad, provider):
    fields = ({"prompt_eval_count": bad, "eval_count": bad, "prompt_eval_cached_count": bad,
               "eval_duration": bad} if provider == "ollama" else
              {"usage": {"prompt_tokens": bad, "completion_tokens": bad, "total_tokens": bad,
                         "prompt_tokens_details": {"cached_tokens": bad}}})
    result = TextClient(provider=provider, model="fixed", opener=Opener(response(provider, **fields))).infer(ask())
    assert all(value is None for value in result["usage"].values())


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_impossible_cached_prompt_count_stays_unknown(provider):
    fields = ({"prompt_eval_cached_count": 21} if provider == "ollama" else
              {"usage": {"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 21}}})
    result = TextClient(provider=provider, model="fixed", opener=Opener(response(provider, **fields))).infer(ask())
    assert result["usage"]["cached_prompt_tokens"] is None


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_reported_output_above_call_allowance_fails_with_per_call_evidence(provider):
    client = TextClient(provider=provider, model="fixed", opener=Opener(response(provider)))
    with pytest.raises(TextInferenceError, match="above the requested limit") as caught:
        client.infer(ask(tokens=9))
    assert caught.value.exchange["usage"]["completion_tokens"] == 10
    assert caught.value.exchange["reserved_output_tokens"] == 9
    assert caught.value.provider_may_be_running is False


@pytest.mark.parametrize("messages", [[], {}, [{"role": "tool", "content": "x"}],
    [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
    [{"role": "user", "content": "x", "name": "extra"}], [{"role": "user"}],
    [{"role": "user", "content": "\ud800"}], [{"role": True, "content": "x"}],
    [{"role": "user", "content": "x"}] * 4097])
def test_only_bounded_role_content_messages_are_accepted(messages):
    with pytest.raises(ValueError):
        TextRequest(messages, 64)


@pytest.mark.parametrize("tokens", [0, -1, True, 1.5, 131073, "64"])
def test_token_allowance_requires_a_bounded_integer(tokens):
    with pytest.raises(ValueError):
        ask(tokens=tokens)


@pytest.mark.parametrize("temperature", [True, -1, 2.01, float("nan"), float("inf"), "0"])
def test_temperature_is_optional_finite_and_bounded(temperature):
    with pytest.raises(ValueError):
        ask(temperature=temperature)


def test_actual_utf8_encoded_request_limit_prevents_sending_and_bounds_error_record():
    opener = Opener()
    client = TextClient(provider="ollama", model="fixed", opener=opener, max_request_bytes=256)
    with pytest.raises(TextInferenceError, match="max_request_bytes") as caught:
        client.infer(ask("矮" * 100))
    exchange = caught.value.exchange
    assert exchange["request_bytes"] > 300
    assert exchange["request"] is None and exchange["request_truncated"] is True
    assert len(exchange["request_text"].encode("utf-8")) <= 256
    assert exchange["request_sent"] is False
    assert caught.value.provider_may_be_running is False
    assert opener.requests == []


@pytest.mark.parametrize("raw", [b"x" * 257, b"\xff" * 257, ("界" * 100).encode()])
def test_response_prefix_and_replacement_unicode_obey_actual_byte_limit(raw):
    client = TextClient(provider="ollama", model="fixed", opener=Opener(raw), max_response_bytes=256)
    with pytest.raises(TextInferenceError, match="max_response_bytes") as caught:
        client.infer(ask())
    exchange = caught.value.exchange
    assert exchange["response_bytes"] == 256 and exchange["response_truncated"] is True
    assert len(exchange["response_text"].encode()) <= 256
    assert exchange["response_complete"] is False
    assert caught.value.provider_may_be_running is True


@pytest.mark.parametrize("raw", [b"not-json", b"\xff", b'{"message":{},"message":{}}', b'{"number":NaN}'])
def test_invalid_json_or_utf8_fails_without_turning_into_a_text_result(raw):
    client = TextClient(provider="ollama", model="fixed", opener=Opener(raw))
    with pytest.raises(TextInferenceError, match="valid UTF-8 JSON") as caught:
        client.infer(ask())
    assert caught.value.exchange["response_complete"] is True


@pytest.mark.parametrize("message", [{"role": "user", "content": "x"}, {"role": "assistant", "content": None},
    {"role": "assistant", "content": "x", "tool_calls": [{}]},
    {"role": "assistant", "content": "x", "images": ["image"]},
    {"role": "assistant", "content": "x", "function_call": {"name": "f"}}])
def test_provider_tool_or_nontext_output_is_not_executed_or_reinterpreted(message):
    client = TextClient(provider="ollama", model="fixed", opener=Opener(response(message=message)))
    with pytest.raises(TextInferenceError, match="assistant content text"):
        client.infer(ask())


@pytest.mark.parametrize("fields", [{"done": False}, {"done_reason": "tool_calls"}, {"done_reason": None}])
def test_native_response_requires_a_completed_recognized_finish(fields):
    client = TextClient(provider="ollama", model="fixed", opener=Opener(response(**fields)))
    with pytest.raises(TextInferenceError):
        client.infer(ask())


@pytest.mark.parametrize("choices", [[], [{}, {}], None, ["text"]])
def test_compatible_response_requires_one_choice(choices):
    client = TextClient(provider="chat_completions", model="fixed", opener=Opener(response("chat_completions", choices=choices)))
    with pytest.raises(TextInferenceError, match="exactly one"):
        client.infer(ask())


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_credential_echo_including_json_escapes_is_omitted_and_rejected(monkeypatch, provider):
    secret = "fixture-secret-token"
    monkeypatch.setenv("INFERENCE_TEST_KEY", secret)
    raw = response(provider, text=secret).replace(secret.encode(), "".join(f"\\u{ord(char):04x}" for char in secret).encode())
    opener = Opener(raw)
    client = TextClient(provider=provider, model="fixed", api_key_env="INFERENCE_TEST_KEY", opener=opener)
    with pytest.raises(TextInferenceError, match="echoed a credential") as caught:
        client.infer(ask())
    assert opener.requests[0][0].get_header("Authorization") == "Bearer " + secret
    assert caught.value.exchange["response_text"] is None
    assert caught.value.exchange["response_omitted_reason"] == "credential_echo"
    assert secret not in str(caught.value) + json.dumps(caught.value.exchange) + json.dumps(client.public_config())


def test_credential_in_input_is_not_sent_or_copied_to_error_evidence(monkeypatch):
    secret = "fixture-secret-token"
    monkeypatch.setenv("INFERENCE_TEST_KEY", secret)
    opener = Opener()
    client = TextClient(provider="ollama", model="fixed", api_key_env="INFERENCE_TEST_KEY", opener=opener)
    with pytest.raises(TextInferenceError, match="input was not sent") as caught:
        client.infer(ask(secret))
    assert opener.requests == []
    assert secret not in json.dumps(caught.value.exchange)


@pytest.mark.parametrize("raw", [b'{"error":"private provider detail"}', b"[]"])
def test_api_error_envelopes_are_omitted(raw):
    client = TextClient(provider="ollama", model="fixed", opener=Opener(raw))
    with pytest.raises(TextInferenceError) as caught:
        client.infer(ask())
    assert caught.value.exchange["response_text"] is None
    assert "private" not in str(caught.value) + json.dumps(caught.value.exchange)


def test_missing_key_is_rejected_before_transport(monkeypatch):
    monkeypatch.delenv("INFERENCE_ABSENT_TEST_KEY", raising=False)
    opener = Opener()
    client = TextClient(provider="ollama", model="fixed", api_key_env="INFERENCE_ABSENT_TEST_KEY", opener=opener)
    with pytest.raises(TextInferenceError, match="empty or missing") as caught:
        client.infer(ask())
    assert opener.requests == [] and caught.value.exchange["request_sent"] is False


@pytest.mark.parametrize("failure", [error.URLError("private transport detail"), OSError("private details")])
def test_transport_error_has_safe_per_call_evidence_and_no_retry(failure):
    opener = Opener(failure)
    client = TextClient(provider="ollama", model="fixed", opener=opener)
    with pytest.raises(TextInferenceError) as caught:
        client.infer(ask("Caller context"))
    assert "private" not in str(caught.value) + json.dumps(caught.value.exchange)
    assert len(opener.requests) == 1
    assert caught.value.provider_may_be_running is True


@pytest.mark.parametrize("kwargs", [{"endpoint": "http://remote.example/api/chat"},
    {"endpoint": "http://user:pass@localhost/api/chat"}, {"endpoint": "http://localhost/api/chat?key=x"},
    {"endpoint": "http://localhost/api/chat#x"}, {"endpoint": "http://localhost:99999/api/chat"},
    {"endpoint": "http://localhost/../api/chat"}, {"endpoint": "http://localhost/%2e%2e/api/chat"},
    {"endpoint": "http://local\nhost/api/chat"}, {"endpoint": "http://localhost/api/generate"},
    {"endpoint": "file:///api/chat"}, {"mode": "cloud"}, {"model": "name:cloud"},
    {"num_ctx": True}, {"num_ctx": 0}, {"token_limit_field": "max_tokens"},
    {"max_request_bytes": True}, {"max_response_bytes": 0}, {"timeout": float("nan")},
    {"max_output_tokens": 0}, {"api_key_env": "invalid-name"}, {"model": "\ud800"}])
def test_native_operator_configuration_rejects_unsafe_or_unsupported_settings(kwargs):
    with pytest.raises(ValueError):
        TextClient(**{"provider": "ollama", "model": "fixed", **kwargs})


def test_cloud_compatible_requires_explicit_https_and_key_name_without_loading_key():
    with pytest.raises(ValueError):
        TextClient(provider="chat_completions", model="fixed", mode="cloud")
    with pytest.raises(ValueError):
        TextClient(provider="chat_completions", model="fixed", mode="cloud", endpoint="http://example.com/v1/chat/completions", api_key_env="KEY_NAME")
    client = TextClient(provider="chat_completions", model="fixed", mode="cloud", endpoint="https://example.com/v1/chat/completions", api_key_env="KEY_NAME")
    assert client.public_config()["token_limit_field"] == "max_completion_tokens"


def test_operator_options_public_copy_and_call_timeout_are_fixed():
    opener = Opener()
    client = TextClient(provider="ollama", model="fixed", num_ctx=32768, timeout=2, opener=opener)
    config = client.public_config()
    config.update(model="changed", num_ctx=1)
    client.infer(ask(), timeout=1)
    client.infer(ask(), timeout=3)
    assert [timeout for _, timeout in opener.requests] == [1, 2]
    assert json.loads(opener.requests[0][0].data)["options"] == {"num_predict": 64, "num_ctx": 32768}
    assert client.model == "fixed" and client.public_config()["num_ctx"] == 32768
    with pytest.raises(ValueError):
        client.infer(ask(tokens=32769))
    with pytest.raises(ValueError):
        client.infer({"messages": [], "model": "override"})


def test_wall_deadline_does_not_wait_for_a_trickling_reader_or_publish_late_results():
    release = threading.Event()
    finished = threading.Event()

    class SlowOpener:
        def open(self, req, timeout):
            release.wait(2)
            finished.set()
            return io.BytesIO(response())

    client = TextClient(provider="ollama", model="fixed", opener=SlowOpener())
    started = time.monotonic()
    try:
        with pytest.raises(TextInferenceError, match="wall-time allowance") as caught:
            client.infer(ask(), timeout=0.02)
        assert time.monotonic() - started < 1
        assert caught.value.provider_may_be_running is True
        witness = json.dumps(caught.value.exchange)
    finally:
        release.set()
        assert finished.wait(2)
    assert json.dumps(caught.value.exchange) == witness
    with pytest.raises(TextInferenceError, match="admission is closed") as rejected:
        client.infer(ask("Another context"))
    assert rejected.value.exchange["request_sent"] is False


@pytest.mark.parametrize("kind", ["objective", "request", "config", "key"])
def test_public_input_preflight_rejects_credentials_before_broker_persistence(monkeypatch, kind):
    secret = "fixture-secret-token"
    monkeypatch.setenv("INFERENCE_TEST_KEY", secret)
    opener = Opener()
    client = TextClient(provider="ollama", model="fixed", api_key_env="INFERENCE_TEST_KEY", opener=opener)
    values = {"objective": "Objective " + secret, "request": ask(secret).to_dict(),
              "config": {"nested": [{"model": secret}]}, "key": {secret: "value"}}
    with pytest.raises(ValueError, match="configured credential") as caught:
        client.check_public_input(values[kind])
    assert secret not in str(caught.value)
    assert opener.requests == []
    assert client.check_public_input({"objective": "My own objective", "api_key_env": "INFERENCE_TEST_KEY"}) is None


def test_response_overflow_closes_future_direct_client_admission():
    opener = Opener(b"x" * 257)
    client = TextClient(provider="ollama", model="fixed", opener=opener, max_response_bytes=256)
    with pytest.raises(TextInferenceError, match="max_response_bytes"):
        client.infer(ask())
    with pytest.raises(TextInferenceError, match="admission is closed"):
        client.infer(ask())
    assert len(opener.requests) == 1


@pytest.fixture
def local_endpoint():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
            text = body["messages"][-1]["content"]
            if text == "redirect":
                self.send_response(307)
                self.send_header("Location", "/credential-sink")
                raw = b""
            elif text == "http_error":
                self.send_response(401)
                raw = b"fixture-secret-token private error body"
            else:
                self.send_response(200)
                raw = response("ollama" if self.path.endswith("/api/chat") else "chat_completions", text="Reply: " + text)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_concurrent_http_contexts_have_distinct_results_and_exact_witnesses(local_endpoint, provider):
    base, received = local_endpoint
    client = TextClient(provider=provider, model="fixed", endpoint=base + ("/api/chat" if provider == "ollama" else "/v1/chat/completions"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda text: client.infer(ask(text)), ["one", "two", "three", "four"]))
    for text, result in zip(["one", "two", "three", "four"], results):
        assert result["text"] == "Reply: " + text
        assert result["exchange"]["request"]["messages"] == [{"role": "user", "content": text}]
        assert "Reply: " + text in result["exchange"]["response_text"]
    assert len(received) == 4


@pytest.mark.parametrize("behavior", ["normal", "redirect", "http_error"])
def test_real_http_disables_proxy_discovery_redirects_and_credential_error_leaks(local_endpoint, monkeypatch, behavior):
    base, received = local_endpoint
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("INFERENCE_TEST_KEY", "fixture-secret-token")
    client = TextClient(provider="chat_completions", model="fixed", endpoint=base + "/v1/chat/completions", api_key_env="INFERENCE_TEST_KEY", timeout=2)
    if behavior == "normal":
        result = client.infer(ask(behavior))
        witness = result["exchange"]
    else:
        with pytest.raises(TextInferenceError, match="redirects|HTTP") as caught:
            client.infer(ask(behavior))
        witness = caught.value.exchange
        assert "fixture-secret-token" not in str(caught.value)
    assert len(received) == 1
    assert received[0]["authorization"] == "Bearer fixture-secret-token"
    assert received[0]["path"] == "/v1/chat/completions"
    assert "fixture-secret-token" not in json.dumps(witness) + json.dumps(client.public_config())
