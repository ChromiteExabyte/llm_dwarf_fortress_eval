"""Exercise the public repeat workflow with fake saves and a fake native bridge."""

import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from urllib import request

import pytest

from dfeval import cli, experiment, repeat, scenario
from dfeval.local_models import OllamaPolicy
from dfeval.model_observation import MODEL_OBSERVATION_VERSION
from dfeval.policies import ChatCompletionsPolicy, IdlePolicy, RulePolicy, json_bytes
from test_repeat import fixture


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: [])
    monkeypatch.chdir(tmp_path)
    return fixture(tmp_path)


def prepare(source, tmp_path):
    game, _, _, run, _ = source
    output = tmp_path / "model b"
    assert cli.main(["repeat", "prepare", "--from", str(run), "--model", "new-model",
                     "--out", str(output), "--df-path", str(game), "--game-stopped"]) == 0
    return output


def test_cli_scenario_capture_and_restore_accept_explicit_save_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: [])
    game = tmp_path / "game"
    game.mkdir()
    (game / "release notes.txt").write_text("Release notes for 53.16\n")
    save_root = tmp_path / "AppData" / "Roaming" / "Bay 12 Games" / "Dwarf Fortress" / "save"
    saved = save_root / "region1"
    saved.mkdir(parents=True)
    (saved / "world.sav").write_bytes(b"saved checkpoint")
    snapshot = tmp_path / "snapshot"
    assert cli.main(["scenario", "capture", "--df-path", str(game), "--save-root", str(save_root),
                     "--save-name", "region1", "--out", str(snapshot), "--game-stopped"]) == 0
    assert "save_root" not in json.loads(capsys.readouterr().out)
    assert cli.main(["scenario", "restore", str(snapshot), "--df-path", str(game),
                     "--save-root", str(save_root), "--save-name", "replay", "--game-stopped"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["save_root"] == str(save_root)
    assert (save_root / "replay" / "world.sav").read_bytes() == b"saved checkpoint"
    assert not (game / "save").exists()


def test_cli_repeat_prepare_persists_external_root_and_full_native_version(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: [])
    save_root = tmp_path / "AppData" / "Roaming" / "Bay 12 Games" / "Dwarf Fortress" / "save"
    game, saved, _, run, _ = fixture(tmp_path, save_root=save_root, native_version="v0.53.16 win64 ITCH")
    output = tmp_path / "repeat"
    assert cli.main(["repeat", "prepare", "--from", str(run), "--model", "different-model",
                     "--out", str(output), "--df-path", str(game), "--save-root", str(save_root),
                     "--game-stopped"]) == 0
    plan = repeat.load_repeat(output)
    assert plan["resolved_paths"]["save_root"] == str(save_root)
    assert plan["resolved_paths"]["restored_save_dir"] == str(saved)
    assert plan["initial_expectation"]["df_version"] == "v0.53.16 win64 ITCH"


def fake_execution(monkeypatch, output, *, changed_start=False, forbid_model=False):
    initial = json.loads((output / "source" / "initial.json").read_text())
    if changed_start:
        initial["absolute_tick"] += 1
    calls = []
    seen = []

    class Bridge:
        session = "fake-native-session"
        timeout = 1

        def install_script(self):
            return Path(experiment.__file__).parent / "bridge" / "lua" / "dfeval-live.lua"

        def status(self):
            calls.append("status")
            return copy.deepcopy(initial)

        def pause(self):
            calls.append("pause")
            return {"paused": True}

        def observe(self):
            calls.append("observe")
            return copy.deepcopy(initial)

        def set_simulation_fps(self, fps):
            calls.append(("speed", fps))
            return {"simulation_fps": {"effective": fps}}

        def restore_simulation_fps(self):
            calls.append("restore_speed")
            return {"restored": True}

        def queue_brew(self, workshop_id, quantity):
            calls.append(("brew", workshop_id, quantity))
            workshop = next(item for item in initial["workshops"] if item["id"] == workshop_id)
            assert workshop["jobs"] == []
            # The fake job stays queued: this test makes no production/completion claim.
            workshop["jobs"] = [{"id": 20, "type": "BrewDrink"}]
            return {"workshop_id": workshop_id, "queued": quantity}

        def advance_ticks(self, ticks, timeout):
            calls.append(("advance", ticks))
            start = initial["absolute_tick"]
            initial["absolute_tick"] += ticks
            return {"paused": True, "elapsed_ticks": ticks, "start_absolute_tick": start,
                    "absolute_tick": initial["absolute_tick"]}

    real_run = experiment.run_experiment

    def run(game, policy, **kwargs):
        seen.append((policy.public_config(), kwargs))
        return real_run(game, policy, **kwargs, bridge_factory=lambda *a, **k: Bridge(),
                        bootstrap=lambda *a: {"returncode": 0})

    def choose(policy, observation, history):
        if forbid_model:
            pytest.fail("A control repeat reached a model provider")
        calls.append("model")
        answer = {"action": "finish", "reason": "Synthetic workflow test", "notebook": ""}
        policy.last_exchange = {"response_text": json_bytes(answer).decode(),
                                "response_bytes": len(json_bytes(answer)),
                                "usage": {"prompt_tokens": 90, "completion_tokens": 20}}
        return answer

    monkeypatch.setattr(experiment, "run_experiment", run)
    monkeypatch.setattr(OllamaPolicy, "choose", choose)
    monkeypatch.setattr(ChatCompletionsPolicy, "choose", choose)
    return calls, seen


def test_cli_repeat_runs_frozen_settings_and_exports_pair(source, tmp_path, monkeypatch, capsys):
    output = prepare(source, tmp_path)
    calls, seen = fake_execution(monkeypatch, output)
    # A newly changed setup must never become the repeat's configuration.
    (tmp_path / "dfeval.local.toml").write_text('[benchmark]\nmodel="wrong-model"\ntimeout=1\n')
    assert cli.main(["repeat", "run", str(output)]) == 0
    policy, options = seen[0]
    assert policy["model"] == "new-model"
    assert policy["options"] == source[-1]["policy"]["options"]
    assert policy["max_completion_tokens"] == 333
    assert options["config"].history_decisions == 3
    assert options["config"].max_input_bytes == 543210
    assert options["config"].max_snapshot_bytes == 500000
    assert options["config"].max_log_bytes == 4000000
    assert options["config"].ticks_per_decision == 17
    assert options["config"].simulation_fps == 333
    assert calls.count("model") == 1
    assert calls[-2:] == ["pause", "restore_speed"]
    result = json.loads((output / "run" / "result.json").read_text())
    assert result["initial_state_check"]["matched"] is True
    manifest = json.loads((output / "run" / "manifest.json").read_text())
    assert (output / "run" / manifest["starting_snapshot_path"]).resolve() == source[2]
    assert Path(manifest["config"]["stop_file"]) == output / "run" / "STOP"
    report = json.loads((output / "comparison" / "benchmark.json").read_text())
    assert len(report["runs"]) == 2
    assert (output / "comparison" / "runs.csv").is_file()
    assert (output / "run" / "benchmark" / "README.md").is_file()
    assert "Paired comparison:" in capsys.readouterr().out


def test_cli_refuses_changed_loaded_site_before_model(source, tmp_path, monkeypatch):
    output = prepare(source, tmp_path)
    calls, _ = fake_execution(monkeypatch, output, changed_start=True)
    assert cli.main(["repeat", "run", str(output)]) == 1
    assert "model" not in calls
    result = json.loads((output / "run" / "result.json").read_text())
    assert result["budgets"]["policy_calls"] == 0
    assert result["budgets"]["requested_ticks"] == 0
    assert result["pause_confirmed"] is True
    assert result["initial_state_check"]["matched"] is False


@pytest.mark.parametrize("control,policy_type", [("idle", IdlePolicy), ("rule", RulePolicy)])
def test_cli_control_repeat_runs_frozen_budget_and_guard_without_cloud_key(
        tmp_path, monkeypatch, control, policy_type):
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: [])
    monkeypatch.chdir(tmp_path)
    game, _, snapshot, source, _ = fixture(tmp_path, provider="cloud", workshops=[
        {"id": 9, "type": "Still", "completed": True, "jobs": []}])
    monkeypatch.delenv("DFEVAL_REPEAT_TEST_KEY", raising=False)
    monkeypatch.setattr(request.OpenerDirector, "open", lambda *a, **kw: pytest.fail("Control made an HTTP request"))
    output = tmp_path / "control repeat"
    assert cli.main(["repeat", "prepare", "--from", str(source), "--control", control,
                     "--out", str(output), "--df-path", str(game), "--game-stopped"]) == 0
    plan = repeat.load_repeat(output)
    calls, seen = fake_execution(monkeypatch, output, forbid_model=True)
    (tmp_path / "dfeval.local.toml").write_text('[benchmark]\nmodel="wrong-model"\ntimeout=1\n')
    assert cli.main(["repeat", "run", str(output)]) == 0
    policy, options = seen[0]
    assert policy == policy_type().public_config()
    assert asdict(options["config"]) == plan["config"]
    assert options["initial_expectation"] == plan["initial_expectation"]
    assert "model" not in calls
    assert [call for call in calls if isinstance(call, tuple) and call[0] == "advance"] == [("advance", 17)] * 3
    assert [call for call in calls if isinstance(call, tuple) and call[0] == "brew"] == (
        [("brew", 9, 1)] if control == "rule" else [])
    assert calls[-2:] == ["pause", "restore_speed"]
    result = json.loads((output / "run" / "result.json").read_text())
    assert result["outcome"] == "budget_exhausted"
    assert result["detail"] == "max_decisions"
    assert result["initial_state_check"]["matched"] is True
    assert result["budgets"]["decisions"] == result["budgets"]["policy_calls"] == 3
    assert result["budgets"]["requested_ticks"] == result["budgets"]["reported_elapsed_ticks"] == 51
    assert result["budgets"]["output_tokens_reserved"] == 0
    assert result["budgets"]["reported_prompt_tokens"] == result["budgets"]["reported_completion_tokens"] == 0
    assert result["pause_confirmed"] is True
    assert result["speed_restored"] is True
    manifest = json.loads((output / "run" / "manifest.json").read_text())
    assert manifest["policy"] == policy
    assert Path(manifest["config"]["stop_file"]) == output / "run" / "STOP"
    assert (output / "run" / manifest["starting_snapshot_path"]).resolve() == snapshot
    events = [json.loads(line) for line in (output / "run" / "events.jsonl").read_text().splitlines()]
    inputs = [event for event in events if event["kind"] == "policy_input"]
    assert len(inputs) == 3
    assert all(event["model_observation_version"] == MODEL_OBSERVATION_VERSION for event in inputs)
    guard = next(event for event in events if event["kind"] == "initial_state_check")
    assert guard["event"] < inputs[0]["event"]
    decisions = [event["decision"]["action"] for event in events if event["kind"] == "decision"]
    assert decisions == (["brew", "wait", "wait"] if control == "rule" else ["wait"] * 3)
    report = json.loads((output / "comparison" / "benchmark.json").read_text())
    assert len(report["runs"]) == 2


@pytest.mark.parametrize("control,policy_type", [("idle", IdlePolicy), ("rule", RulePolicy)])
def test_cli_control_refuses_wrong_start_before_policy_or_gameplay(
        source, tmp_path, monkeypatch, control, policy_type):
    output = tmp_path / "control"
    assert cli.main(["repeat", "prepare", "--from", str(source[3]), "--control", control,
                     "--out", str(output), "--df-path", str(source[0]), "--game-stopped"]) == 0
    calls, _ = fake_execution(monkeypatch, output, changed_start=True, forbid_model=True)
    monkeypatch.setattr(policy_type, "choose", lambda *a, **kw: pytest.fail("Control ran before the save guard"))
    assert cli.main(["repeat", "run", str(output)]) == 1
    assert not any(isinstance(call, tuple) and call[0] in ("advance", "brew") for call in calls)
    result = json.loads((output / "run" / "result.json").read_text())
    assert result["initial_state_check"]["matched"] is False
    assert result["budgets"]["policy_calls"] == result["budgets"]["decisions"] == 0
    assert result["budgets"]["requested_ticks"] == 0
    assert result["pause_confirmed"] is True
    assert result["speed_restored"] is True


@pytest.mark.parametrize("selection", [[], ["--model", "new-model", "--control", "idle"],
                                      ["--control", "custom"]])
def test_cli_requires_exactly_one_supported_repeat_selection(source, tmp_path, monkeypatch, selection):
    monkeypatch.setattr(repeat, "prepare_repeat", lambda *a, **kw: pytest.fail("Invalid selection reached preparation"))
    with pytest.raises(SystemExit) as failure:
        cli.main(["repeat", "prepare", "--from", str(source[3]), *selection,
                  "--out", str(tmp_path / "repeat"), "--df-path", str(source[0]), "--game-stopped"])
    assert failure.value.code == 2
    assert not (tmp_path / "repeat").exists()


def test_cli_prepare_requires_stopped_attestation(source, tmp_path):
    game, save, _, run, _ = source
    before = (save / "world.sav").read_bytes()
    assert cli.main(["repeat", "prepare", "--from", str(run), "--model", "new-model",
                     "--out", str(tmp_path / "repeat"), "--df-path", str(game)]) == 1
    assert (save / "world.sav").read_bytes() == before


def test_cli_repeat_rejects_parameter_overrides(source, tmp_path):
    output = prepare(source, tmp_path)
    with pytest.raises(SystemExit) as failure:
        cli.main(["repeat", "run", str(output), "--ticks-per-decision", "999"])
    assert failure.value.code == 2


def test_changed_original_evidence_does_not_enter_paired_report(source, tmp_path, monkeypatch, capsys):
    output = prepare(source, tmp_path)
    fake_execution(monkeypatch, output)
    events = source[3] / "events.jsonl"
    events.write_bytes(events.read_bytes() + b"\n")
    assert cli.main(["repeat", "run", str(output)]) == 0
    assert not (output / "comparison").exists()
    assert (output / "run" / "benchmark" / "benchmark.json").is_file()
    assert "Original source recording changed" in capsys.readouterr().err


def test_cli_first_run_records_relative_checkpoint_reference(source, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(experiment, "run_experiment", lambda *a, **kw: seen.append(kw) or {"ok": True})
    out = tmp_path / "first"
    assert cli.main(["experiment", "--policy", "idle", "--starting-snapshot", str(source[2]),
                     "--out", str(out)]) == 0
    assert (out / seen[0]["starting_snapshot_path"]).resolve() == source[2]
    assert seen[0]["config"].starting_save_sha256 == hashlib.sha256((source[2] / "manifest.json").read_bytes()).hexdigest()


def test_prepare_uses_saved_game_path_without_reusing_model_selection(source, tmp_path, monkeypatch):
    (tmp_path / "dfeval.local.toml").write_text('[benchmark]\ndf_path="game"\nmodel="wrong-model"\n')
    monkeypatch.delenv("DFEVAL_DF_PATH", raising=False)
    output = tmp_path / "repeat"
    assert cli.main(["repeat", "prepare", "--from", str(source[3]), "--model", "new-model",
                     "--out", str(output), "--game-stopped"]) == 0
    assert repeat.load_repeat(output)["model"] == "new-model"


def test_cloud_repeat_preserves_endpoint_and_requires_key_before_game(source, tmp_path, monkeypatch, capsys):
    original_run, manifest = source[3:]
    cloud = ChatCompletionsPolicy(mode="cloud", model="original-model",
        endpoint="https://models.example/v1/chat/completions", api_key_env="DFEVAL_REPEAT_TEST_KEY",
        max_completion_tokens=333, timeout=47, max_response_bytes=32768,
        max_request_bytes=543210, response_format="json_schema")
    manifest["policy"] = manifest["config"]["policy"] = cloud.public_config()
    (original_run / "manifest.json").write_bytes(json_bytes(manifest))
    events = [json.loads(line) for line in (original_run / "events.jsonl").read_text().splitlines()]
    events[0]["config"] = manifest["config"]
    (original_run / "events.jsonl").write_bytes(b"".join(json_bytes(event) + b"\n" for event in events))
    output = prepare(source, tmp_path)
    calls, seen = fake_execution(monkeypatch, output)
    monkeypatch.delenv("DFEVAL_REPEAT_TEST_KEY", raising=False)
    assert cli.main(["repeat", "run", str(output)]) == 1
    assert not calls and not seen
    assert "Set the DFEVAL_REPEAT_TEST_KEY" in capsys.readouterr().err
    monkeypatch.setenv("DFEVAL_REPEAT_TEST_KEY", "repeat-test-credential")
    assert cli.main(["repeat", "run", str(output)]) == 0
    assert seen[0][0]["endpoint"] == cloud.public_config()["endpoint"]
    assert seen[0][0]["api_key_env"] == "DFEVAL_REPEAT_TEST_KEY"
    assert "repeat-test-credential" not in (output / "repeat.json").read_text()
    assert "repeat-test-credential" not in (output / "run" / "manifest.json").read_text()
