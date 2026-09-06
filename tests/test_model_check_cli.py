"""CLI model preflight checks with injected HTTP responses and no native game."""

import io
import json
import subprocess
from urllib import error

import pytest

from dfeval import cli, model_check, policies


DECISION = {"action": "finish", "reason": "Synthetic connection check received.", "notebook": ""}


class Opener:
    def __init__(self, raw=None):
        self.raw = raw if raw is not None else json.dumps({
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(DECISION)}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }).encode()
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.raw, Exception):
            raise self.raw
        return io.BytesIO(self.raw)


@pytest.fixture
def offline(monkeypatch):
    opener = Opener()
    monkeypatch.setattr(policies.request, "build_opener", lambda *args: opener)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No child process may run in model preflight"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("No game or model server may be launched"))
    return opener


def test_local_cli_invokes_transport_and_writes_only_synthetic_report(offline, tmp_path, capsys):
    out = tmp_path / "preflight"
    assert cli.main(["model-check", "--policy", "local", "--model", "my-explicit-model",
                     "--endpoint", "http://127.0.0.1:8080/v1/chat/completions",
                     "--response-tokens", "100", "--timeout", "3", "--out", str(out)]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert report["kind"] == "model_connection_check" and report["synthetic"] is True
    assert report["decision"] == DECISION and report["game_access"] is False
    assert report["actions_executed"] == 0
    assert json.loads((out / "model-check.json").read_text()) == report
    assert len(list(out.iterdir())) == 1 and len(offline.requests) == 1
    request, timeout = offline.requests[0]
    body = json.loads(request.data)
    assert request.full_url == "http://127.0.0.1:8080/v1/chat/completions"
    assert body["model"] == "my-explicit-model" and body["max_tokens"] == 100
    assert timeout == 3 and request.get_header("Authorization") is None
    assert json.loads(body["messages"][1]["content"])["observation"]["citizens"] is None


def test_local_cli_default_endpoint_is_explicit_in_public_report(offline, capsys):
    assert cli.main(["model-check", "--policy", "local", "--model", "chosen-model"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["policy"]["endpoint"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert report["output_directory"] is None
    assert len(offline.requests) == 1


def test_cli_records_explicit_json_schema_mode(offline, capsys):
    assert cli.main(["model-check", "--policy", "local", "--model", "chosen-model",
                     "--response-format", "json_schema"]) == 0
    report = json.loads(capsys.readouterr().out)
    sent = json.loads(offline.requests[0][0].data)["response_format"]
    assert sent["type"] == "json_schema"
    assert report["policy"]["response_format"] == sent
    assert report["decision"] == DECISION


def test_cloud_cli_uses_selected_endpoint_key_name_and_token_field(offline, monkeypatch, capsys):
    secret = "cli-preflight-" + "private-test-value"
    monkeypatch.setenv("CLI_PREFLIGHT_KEY", secret)
    assert cli.main(["model-check", "--policy", "cloud", "--model", "chosen-cloud-model",
                     "--endpoint", "https://provider.example/v1/chat/completions",
                     "--api-key-env", "CLI_PREFLIGHT_KEY", "--token-limit-field", "max_tokens",
                     "--response-tokens", "64"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["policy"]["api_key_env"] == "CLI_PREFLIGHT_KEY"
    assert report["policy"]["token_limit_field"] == "max_tokens"
    request = offline.requests[0][0]
    assert request.full_url == "https://provider.example/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer " + secret
    assert json.loads(request.data)["max_tokens"] == 64
    assert secret not in captured.out + captured.err


def test_cloud_defaults_are_reported_and_use_completion_token_field(offline, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-credential")
    assert cli.main(["model-check", "--policy", "cloud", "--model", "explicit-cloud-model"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["policy"]["endpoint"] == "https://api.openai.com/v1/chat/completions"
    assert report["policy"]["api_key_env"] == "OPENAI_API_KEY"
    assert "max_completion_tokens" in json.loads(offline.requests[0][0].data)


def test_cloud_missing_key_fails_before_check_or_transport(offline, monkeypatch, capsys):
    monkeypatch.delenv("ABSENT_PREFLIGHT_CLI_KEY", raising=False)
    monkeypatch.setattr(model_check, "check_model", lambda *a, **k: pytest.fail("Missing credentials must fail before check"))
    assert cli.main(["model-check", "--policy", "cloud", "--model", "explicit-model",
                     "--api-key-env", "ABSENT_PREFLIGHT_CLI_KEY"]) == 1
    assert "Set the ABSENT_PREFLIGHT_CLI_KEY" in capsys.readouterr().err
    assert offline.requests == []


@pytest.mark.parametrize("arguments", [
    ["model-check", "--policy", "local"],
    ["model-check", "--model", "chosen-model"],
    ["model-check", "--policy", "idle", "--model", "chosen-model"],
    ["model-check", "--policy", "local", "--model", "chosen-model", "--df-path", "game"],
    ["model-check", "--policy", "local", "--model", "chosen-model", "--token-limit-field", "unknown"],
])
def test_parser_rejects_missing_selection_or_unsupported_options(offline, arguments):
    with pytest.raises(SystemExit) as failure:
        cli.main(arguments)
    assert failure.value.code == 2 and offline.requests == []


@pytest.mark.parametrize("extra", [
    ["--endpoint", "http://remote.example/v1/chat/completions"],
    ["--endpoint", "http://127.0.0.1:8080/v1/models"],
    ["--timeout", "0"], ["--timeout", "nan"], ["--response-tokens", "0"],
])
def test_invalid_local_configuration_fails_without_request(offline, capsys, extra):
    assert cli.main(["model-check", "--policy", "local", "--model", "chosen-model", *extra]) == 1
    assert "Model check:" in capsys.readouterr().err
    assert offline.requests == []


@pytest.mark.parametrize("raw", [b"malformed response", error.URLError("private transport detail")])
def test_response_failure_returns_nonzero_and_public_failed_report(offline, capsys, raw):
    offline.raw = raw
    assert cli.main(["model-check", "--policy", "local", "--model", "chosen-model"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["ok"] is False and report["decision"] is None
    assert report["error"] and report["actions_executed"] == 0
    assert "private transport detail" not in captured.out + captured.err
    assert len(offline.requests) == 1


def test_output_conflict_is_nonzero_without_a_model_call(offline, tmp_path, capsys):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep existing evidence")
    assert cli.main(["model-check", "--policy", "local", "--model", "chosen-model", "--out", str(tmp_path)]) == 1
    assert "new or empty" in capsys.readouterr().err
    assert sentinel.read_text() == "keep existing evidence"
    assert offline.requests == []
