"""Local setup and benchmark workflow, with native game/provider fixtures."""

import argparse
import io
import json
from pathlib import Path

import pytest

from dfeval import cli, launch, experiment
from test_experiment import Bridge


def args(**kwargs):
    return argparse.Namespace(**kwargs)


@pytest.fixture
def local_server(monkeypatch):
    from dfeval import local_models
    report = {"provider": "ollama", "base_url": "http://127.0.0.1:11434", "models": [
        {"name": "test-model:latest", "id": "test-model:latest", "digest": "a" * 64,
         "details": {"parameter_size": "1B", "quantization_level": "Q4"}}]}
    monkeypatch.setattr(local_models, "discover_models", lambda *a, **k: report)
    return report


def test_setup_selects_only_local_model_and_saves_relative_game_path(local_server, tmp_path):
    game = tmp_path / "game folder 矮人"
    game.mkdir()
    target = tmp_path / "dfeval.local.toml"
    output = []
    launch.setup(args(df_path=str(game), out=str(target)), interactive=False, output_fn=output.append)
    text = target.read_text()
    assert str(tmp_path) not in text
    assert launch.load_config(target)["df_path"] == str(game)
    assert launch.load_config(target)["model"] == "test-model:latest"
    assert any("only local model" in value for value in output)
    with pytest.raises(ValueError, match="already exists"):
        launch.write_config(target, {**launch.DEFAULTS, "model": "test-model:latest"})


def test_setup_never_silently_selects_among_multiple_models(local_server, tmp_path):
    local_server["models"].append({"name": "second", "id": "second"})
    with pytest.raises(ValueError, match="Several models"):
        launch.setup(args(df_path=str(tmp_path), out=str(tmp_path / "config.toml")), interactive=False)
    answers = iter(["1", "2"])
    launch.setup(args(df_path=str(tmp_path), out=str(tmp_path / "config.toml")),
                 input_fn=lambda _: next(answers), output_fn=lambda _: None, interactive=True)
    assert launch.load_config(tmp_path / "config.toml")["model"] == "second"


def test_local_config_is_strict_and_config_relative(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text('[benchmark]\ndf_path = "game"\nprovider = "ollama"\n')
    assert launch.load_config(file)["df_path"] == str(tmp_path / "game")
    file.write_text('[benchmark]\napi_key = "should-not-be-stored"\n')
    with pytest.raises(ValueError, match="Unknown configuration"):
        launch.load_config(file)
    file.write_text("x" * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        launch.load_config(file)


def test_explicit_provider_switch_does_not_reuse_ollama_address(tmp_path):
    config = tmp_path / "local.toml"
    config.write_text('[benchmark]\nprovider = "ollama"\nbase_url = "http://127.0.0.1:11434"\nmodel = "chosen"\n')
    selected = launch.settings(args(config=str(config), provider="lmstudio"))
    assert selected["base_url"] == "http://127.0.0.1:1234"
    assert selected["model"] == "chosen"
    unchanged = launch.settings(args(config=str(config), provider="ollama"))
    assert unchanged["base_url"] == "http://127.0.0.1:11434"
    config.write_text('[benchmark]\nprovider = "llamacpp"\nbase_url = "http://127.0.0.1:8081"\nmodel = "chosen"\n')
    assert launch.settings(args(config=str(config), provider="llamacpp"))["base_url"] == "http://127.0.0.1:8081"
    with pytest.raises(ValueError, match="not found"):
        launch.settings(args(config=str(tmp_path / "missing.toml")))


@pytest.mark.parametrize("provider,base,endpoint", [
    ("lmstudio", "http://127.0.0.1:1234", "http://127.0.0.1:1234/v1/chat/completions"),
    ("llamacpp", "http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1/chat/completions"),
])
def test_compatible_presets_resolve_without_provider_sdk(provider, base, endpoint):
    policy = launch.make_policy({**launch.DEFAULTS, "provider": provider, "base_url": base, "model": "chosen"})
    assert policy.public_config()["endpoint"] == endpoint


def test_benchmark_creates_report_from_actual_runner_contract(local_server, monkeypatch, tmp_path, capsys):
    from dfeval import local_models
    game = tmp_path / "game"
    game.mkdir()
    bridge = Bridge(game, 1)
    real_run = experiment.run_experiment
    monkeypatch.setattr(experiment, "run_experiment", lambda *a, **kw: real_run(*a, **kw,
        bridge_factory=lambda *a, **kw: bridge, bootstrap=lambda *a: {"returncode": 0}))
    actual_policy = local_models.OllamaPolicy

    class Response:
        def open(self, request, timeout):
            payload = json.loads(request.data)
            assert payload["model"] == "test-model:latest" and payload["stream"] is False
            return io.BytesIO(json.dumps({"model": payload["model"], "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({"action": "wait", "reason": "test", "notebook": ""})},
                "prompt_eval_count": 100, "eval_count": 20, "eval_duration": 100_000_000,
                "prompt_eval_duration": 200_000_000, "total_duration": 350_000_000, "load_duration": 50_000_000}).encode())

    monkeypatch.setattr(local_models, "OllamaPolicy", lambda **kw: actual_policy(**kw, opener=Response()))
    out = tmp_path / "run"
    monkeypatch.chdir(tmp_path)
    assert cli.main(["benchmark", "--model", "test-model:latest", "--df-path", str(game), "--out", str(out),
                     "--decisions", "1", "--ticks-per-decision", "10", "--max-ticks", "10"]) == 0
    assert bridge.tick == 110 and bridge.calls[-1][0] == "pause"
    assert {"benchmark.json", "runs.csv", "README.md"} <= {p.name for p in (out / "benchmark").iterdir()}
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["policy"]["kind"] == "ollama"
    assert manifest["policy"]["model_identity"]["digest"] == "a" * 64
    assert "Benchmark outputs" in capsys.readouterr().out


def test_models_cli_and_native_ollama_model_check(local_server, monkeypatch, capsys):
    assert cli.main(["models"]) == 0
    assert "test-model:latest" in capsys.readouterr().out
    from dfeval import model_check
    recorded = []
    monkeypatch.setattr(model_check, "check_model", lambda policy, **kw:
        recorded.append(policy.public_config()) or {"ok": True})
    assert cli.main(["model-check", "--provider", "ollama", "--model", "test-model:latest"]) == 0
    assert recorded[0]["kind"] == "ollama"


@pytest.mark.parametrize("extra", [
    ["--endpoint", "http://127.0.0.1:4444/v1/chat/completions"],
    ["--api-key-env", "PRIVATE_KEY"], ["--token-limit-field", "max_tokens"],
    ["--response-format", "json_object"],
])
def test_presets_reject_ignored_connection_options(extra, monkeypatch, capsys):
    from dfeval import model_check
    monkeypatch.setattr(model_check, "check_model", lambda *a, **kw: pytest.fail("Invalid options must fail first"))
    assert cli.main(["model-check", "--provider", "ollama", "--model", "chosen", *extra]) == 1
    assert capsys.readouterr().err


def test_non_ollama_does_not_promise_to_apply_unsupported_seed(monkeypatch, capsys):
    assert cli.main(["model-check", "--provider", "lmstudio", "--model", "chosen", "--seed", "0"]) == 1
    assert "Ollama-specific" in capsys.readouterr().err


def test_no_snapshot_failure_keeps_original_outcome_visible(local_server, monkeypatch, tmp_path, capsys):
    game = tmp_path / "game"
    game.mkdir()
    bridge = Bridge(game, 1)
    real_run = experiment.run_experiment
    monkeypatch.setattr(experiment, "run_experiment", lambda *a, **kw: real_run(*a, **kw,
        bridge_factory=lambda *a, **kw: bridge, bootstrap=lambda *a: {"returncode": 1}))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "failed"
    assert cli.main(["benchmark", "--model", "test-model:latest", "--df-path", str(game), "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert "Outcome: error" in captured.out and "Could not start" in captured.out
    assert "Benchmark export unavailable" in captured.err
    assert (out / "result.json").is_file()


def test_speed_and_call_durations_are_recorded_and_original_cap_restored(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    bridge = Bridge(game, 1)
    called = []
    def set_fps(fps):
        called.append(("set", fps))
        return {"simulation_fps": {"effective": fps}}
    def restore():
        called.append(("restore", 100))
        return {"restored": True}
    bridge.set_simulation_fps = set_fps
    bridge.restore_simulation_fps = restore
    from dfeval.policies import IdlePolicy
    out = tmp_path / "run"
    result = experiment.run_experiment(game, IdlePolicy(), output_dir=out,
        config=experiment.ExperimentConfig(max_decisions=1, ticks_per_decision=10, simulation_fps=1000),
        bridge_factory=lambda *a, **kw: bridge, bootstrap=lambda *a: {"returncode": 0})
    assert result["ok"] and result["speed_restored"] is True
    assert called == [("set", 1000), ("restore", 100)]
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    assert all(event["duration_seconds"] >= 0 for event in events if event["kind"] in ("action_result", "policy_response"))


def test_speed_restore_failure_prevents_success(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    bridge = Bridge(game, 1)
    bridge.set_simulation_fps = lambda fps: {"simulation_fps": {"effective": fps}}
    bridge.restore_simulation_fps = lambda: {"restored": False}
    from dfeval.policies import IdlePolicy
    result = experiment.run_experiment(game, IdlePolicy(), output_dir=tmp_path / "run",
        config=experiment.ExperimentConfig(max_decisions=1, simulation_fps=1000),
        bridge_factory=lambda *a, **kw: bridge, bootstrap=lambda *a: {"returncode": 0})
    assert result["ok"] is False and result["speed_restored"] is False
    assert result["pause_confirmed"] is True
