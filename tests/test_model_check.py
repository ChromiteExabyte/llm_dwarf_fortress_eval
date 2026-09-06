import io
import json
import threading
import time
from urllib import error

import pytest

from dfeval.model_check import check_model
from dfeval.model_observation import MODEL_OBSERVATION_VERSION, model_input_bytes
from dfeval.policies import ChatCompletionsPolicy, IdlePolicy


FINISH = {"action": "finish", "reason": "Synthetic connection check received.", "notebook": ""}


def response(decision=FINISH, *, finish_reason="stop", usage=None):
    return json.dumps({
        "choices": [{"finish_reason": finish_reason, "message": {
            "role": "assistant", "content": json.dumps(decision)}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35} if usage is None else usage,
    }).encode()


class Opener:
    def __init__(self, raw):
        self.raw, self.requests = raw, []

    def open(self, req, timeout):
        self.requests.append((req, timeout))
        if isinstance(self.raw, Exception):
            raise self.raw
        return io.BytesIO(self.raw)


def local(raw=None, **kwargs):
    opener = Opener(response() if raw is None else raw)
    return ChatCompletionsPolicy(mode="local", model="explicit-test-model", opener=opener, **kwargs), opener


def test_check_uses_actual_policy_transport_once_and_records_synthetic_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    policy, opener = local(max_completion_tokens=40, timeout=2)
    report = check_model(policy)
    assert report["ok"] is True
    assert report["kind"] == "model_connection_check"
    assert report["synthetic"] and report["game_access"] is False
    assert report["actions_executed"] == 0 and report["policy_calls"] == 1
    assert report["decision"] == FINISH and report["error"] is None
    assert report["usage"] == {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}
    assert report["wall_seconds"] >= 0
    assert len(opener.requests) == 1
    req, timeout = opener.requests[0]
    payload = json.loads(req.data)
    observed = json.loads(payload["messages"][1]["content"])
    assert observed == {"observation": report["observation"], "history": []}
    assert "SYNTHETIC MODEL CONNECTION CHECK" in observed["observation"]["preflight"]["description"]
    assert all(value is None for key, value in report["observation"].items()
               if key not in ("origin", "preflight", "model_observation_version", "table_columns"))
    assert report["policy"]["model_observation_version"] == MODEL_OBSERVATION_VERSION
    assert payload["messages"][1]["content"].encode() == model_input_bytes(report["observation"], [])
    assert payload["model"] == "explicit-test-model" and payload["max_tokens"] == 40
    assert timeout == 2 and req.get_header("Authorization") is None
    assert list(tmp_path.iterdir()) == []


def test_valid_brew_response_is_data_and_never_executed(tmp_path):
    brew = {**FINISH, "action": "brew", "workshop_id": 1, "quantity": 10}
    policy, opener = local(response(brew))
    out = tmp_path / "check"
    report = check_model(policy, output_dir=out)
    assert report["ok"] and report["decision"] == brew
    assert report["actions_executed"] == 0
    assert list(out.iterdir()) == [out / "model-check.json"]
    assert json.loads((out / "model-check.json").read_text()) == report
    assert len(opener.requests) == 1


@pytest.mark.parametrize("raw", [
    b"not JSON", response({**FINISH, "action": "shell"}), response(finish_reason="length"),
    response(usage={"completion_tokens": 100000}),
    error.URLError("private underlying error"), TimeoutError("private timeout detail"),
    error.HTTPError("https://provider.example", 401, "private credential", {}, io.BytesIO(b"private error body")),
])
def test_connection_or_schema_failure_records_failure_without_retry(tmp_path, raw):
    policy, opener = local(raw)
    out = tmp_path / "failed"
    report = check_model(policy, output_dir=out)
    assert report["ok"] is False and report["decision"] is None
    assert report["error"] and report["actions_executed"] == 0
    assert len(opener.requests) == 1
    assert "private" not in json.dumps(report)
    assert json.loads((out / "model-check.json").read_text())["ok"] is False


def test_missing_cloud_credential_is_checked_before_network(monkeypatch):
    monkeypatch.delenv("MISSING_PREFLIGHT_KEY", raising=False)
    opener = Opener(response())
    policy = ChatCompletionsPolicy(mode="cloud", model="explicit-model",
        endpoint="https://provider.example/v1/chat/completions", api_key_env="MISSING_PREFLIGHT_KEY", opener=opener)
    report = check_model(policy)
    assert not report["ok"] and report["decision"] is None
    assert "environment variable" in report["error"]["message"]
    assert opener.requests == []
    assert report["publicexchange"]["response_text"] is None


def test_credential_echo_is_redacted_and_never_becomes_decision(monkeypatch):
    secret = "preflight-" + "secret-test-value"
    monkeypatch.setenv("PREFLIGHT_TEST_KEY", secret)
    policy, opener = local(response({**FINISH, "notebook": secret}), api_key_env="PREFLIGHT_TEST_KEY")
    report = check_model(policy)
    assert not report["ok"] and report["decision"] is None
    assert secret not in json.dumps(report)
    assert "REDACTED_API_KEY" in report["publicexchange"]["response_text"]
    assert opener.requests[0][0].get_header("Authorization") == "Bearer " + secret


def test_oversized_response_keeps_only_transport_bounded_prefix():
    policy, opener = local(b"x" * 1000, max_response_bytes=256)
    report = check_model(policy)
    assert not report["ok"]
    assert report["publicexchange"]["response_truncated"] is True
    assert len(report["publicexchange"]["response_text"]) == 256
    assert len(opener.requests) == 1


@pytest.mark.parametrize("policy", [IdlePolicy(), object()])
def test_non_model_or_invalid_policy_is_rejected_before_call(policy):
    with pytest.raises((TypeError, ValueError)):
        check_model(policy)


def test_existing_output_is_not_overwritten_or_used_for_a_call(tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("existing evidence")
    policy, opener = local()
    with pytest.raises(ValueError, match="new or empty"):
        check_model(policy, output_dir=out)
    assert sentinel.read_text() == "existing evidence" and opener.requests == []


def test_reused_policy_does_not_report_a_previous_exchange_on_early_failure():
    class Policy:
        last_exchange = {"response_text": "previous unrelated response"}

        def public_config(self):
            return {"is_model": True, "model": "test", "headers": {"Authorization": "private"}}

        def choose(self, observation, history):
            raise RuntimeError("private unexpected exception detail")

    report = check_model(Policy())
    assert not report["ok"] and report["publicexchange"] is None
    assert report["error"]["type"] == "RuntimeError"
    assert "private" not in json.dumps(report)


def test_injected_policy_result_gets_independent_schema_validation_and_public_projection():
    class Policy:
        last_exchange = None

        def public_config(self):
            return {"is_model": True, "model": "test"}

        def choose(self, observation, history):
            observation["citizens"] = [{"id": 7}]
            self.last_exchange = {"response_text": "unsupported decision", "headers": {"Authorization": "private"}}
            return {**FINISH, "command": "arbitrary executable instructions"}

    report = check_model(Policy())
    assert not report["ok"] and report["decision"] is None
    assert report["observation"]["citizens"] is None
    assert report["publicexchange"] == {"response_text": "unsupported decision"}
    assert "private" not in json.dumps(report)


def test_interrupt_returns_a_failed_public_record_without_action(tmp_path):
    class Policy:
        last_exchange = None

        def public_config(self):
            return {"is_model": True, "model": "test"}

        def choose(self, observation, history):
            raise KeyboardInterrupt()

    report = check_model(Policy(), output_dir=tmp_path / "cancelled")
    assert not report["ok"] and report["error"]["type"] == "KeyboardInterrupt"
    assert report["actions_executed"] == 0


def test_overall_deadline_returns_without_waiting_for_a_trickling_endpoint():
    release, finished = threading.Event(), threading.Event()

    class SlowOpener(Opener):
        def open(self, req, timeout):
            self.requests.append((req, timeout))
            try:
                release.wait(5)
                return io.BytesIO(response())
            finally:
                finished.set()

    opener = SlowOpener(response())
    policy = ChatCompletionsPolicy(mode="local", model="explicit-model", opener=opener, timeout=0.03)
    try:
        started = time.monotonic()
        report = check_model(policy)
        assert time.monotonic() - started < 1
        assert not report["ok"] and report["decision"] is None
        assert "wall-time allowance" in report["error"]["message"]
        assert report["background_request_may_continue"] is True
        assert len(opener.requests) == 1
        preserved = json.dumps(report, sort_keys=True)
    finally:
        release.set()
        assert finished.wait(1)
    assert json.dumps(report, sort_keys=True) == preserved
    assert report["actions_executed"] == 0


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_injected_timeout_never_calls_policy(timeout):
    class Policy:
        def public_config(self):
            return {"is_model": True, "model": "test", "timeout": timeout}

        def choose(self, observation, history):
            pytest.fail("Invalid timeout must fail before a model call")

    with pytest.raises(ValueError):
        check_model(Policy())
