"""Repeat saved conditions using tiny fake saves; never call a game or model."""

import copy
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
from urllib import request

import pytest

from dfeval import comparison, repeat, scenario
from dfeval.experiment import ExperimentConfig
from dfeval.environment import capture_environment
from dfeval.local_models import OllamaPolicy
from dfeval.model_observation import MODEL_OBSERVATION_VERSION, projection_contract
from dfeval.policies import (ChatCompletionsPolicy, IdlePolicy, LEGACY_SYSTEM_PROMPT,
                            RulePolicy, SYSTEM_PROMPT, json_bytes)


@pytest.fixture(autouse=True)
def no_game(monkeypatch):
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: [])


def write_json(path, value):
    path.write_bytes(json_bytes(value))


def fixture(tmp_path, *, provider="ollama", outcome="finished", stop=None,
            native_version="53.16", save_root=None, workshops=None, system_prompt=SYSTEM_PROMPT):
    game = tmp_path / "game"
    game.mkdir(parents=True, exist_ok=True)
    save = (Path(save_root) if save_root is not None else game / "save") / "region1"
    save.mkdir(parents=True)
    (save / "world.sav").write_bytes(b"fake starting world")
    (game / "release notes.txt").write_text("Release notes for 53.16\n")
    snapshot = tmp_path / "starting snapshot"
    scenario.snapshot_save(game, "region1", snapshot, game_stopped=True, save_root=save_root)
    (save / "world.sav").write_bytes(b"fake post-run world to preserve")
    source = tmp_path / "source run"
    source.mkdir()
    common = dict(model="original-model", max_completion_tokens=333, timeout=47,
                  max_response_bytes=32768, max_request_bytes=543210, system_prompt=system_prompt)
    if provider == "ollama":
        policy = OllamaPolicy(**common, num_ctx=24576, temperature=0.25, seed=19,
                              keep_alive="4m", think=True,
                              model_identity={"name": "original-model", "digest": "sha256:" + "c" * 64,
                                              "parameter_size": "8B", "quantization_level": "Q4_K_M"})
    elif provider == "cloud":
        policy = ChatCompletionsPolicy(**common, mode="cloud",
                                       endpoint="https://models.example/v1/chat/completions",
                                       api_key_env="DFEVAL_REPEAT_TEST_KEY", response_format="json_schema")
    else:
        policy = ChatCompletionsPolicy(**common, mode="local", endpoint="http://127.0.0.1:8080/v1/chat/completions",
                                       response_format="json_schema", token_limit_field="max_tokens")
    save_hash = hashlib.sha256((snapshot / "manifest.json").read_bytes()).hexdigest()
    config = {**asdict(ExperimentConfig(max_decisions=3, ticks_per_decision=17, max_total_ticks=51,
                                       max_policy_calls=4, max_bridge_calls=24, max_wall_seconds=700,
                                       request_timeout=47, max_input_bytes=543210, max_snapshot_bytes=500000,
                                       max_log_bytes=4000000, history_decisions=3, max_output_tokens=7777,
                                       max_output_bytes=45678, starting_save_sha256=save_hash,
                                       stop_file=str(stop or source / "STOP"), simulation_fps=333)),
              "scenario": "drink-maintenance-v1", "policy": policy.public_config(),
              "model_observation_version": MODEL_OBSERVATION_VERSION}
    initial = {"world_loaded": True, "map_loaded": True, "fortress_mode": True, "paused": True,
               "save_directory": "region1", "absolute_tick": 101, "df_version": native_version,
               "dfhack_version": "53.16-r1.1", "errors": [], "ui_focus": "dwarfmode",
               "simulation_fps": {"effective": 333, "graphics": 50, "original": 100},
               "citizens": [{"id": 1, "dead": False, "stress": 10, "needs": []}],
               "workshops": copy.deepcopy(workshops or []),
               "known_former_citizens": [], "stocks": {"by_item_type": {"DRINK": {"stack_units": 60}}}}
    environment = capture_environment(game)
    events = [{"kind": "run_start", "config": config},
              {"kind": "game_environment", "fingerprint_sha256": environment["fingerprint_sha256"]},
              {"kind": "snapshot", "snapshot": initial}]
    if outcome == "error":
        events.append({"kind": "error", "error": {"type": "DecisionError", "message": "invalid model response"}})
    events.append({"kind": "run_end", "outcome": outcome, "pause_confirmed": True, "speed_restored": True})
    for number, event in enumerate(events):
        event.update(event=number, turn=0, wall_seconds=float(number), at="2026-09-05T12:00:00+00:00")
    (source / "events.jsonl").write_bytes(b"".join(json_bytes(event) + b"\n" for event in events))
    manifest = {"schema_version": 1, "protocol": 1, "bridge_sha256": repeat._bridge_hash(),
                "game_environment": environment,
                "model_observation_version": MODEL_OBSERVATION_VERSION,
                "model_observation": projection_contract(),
                "interface": "native measurements + wait/brew/finish", "events_file": "events.jsonl",
                "config": config, "policy": policy.public_config(), "state": "finished", "outcome": outcome,
                "starting_save_sha256": save_hash, "starting_snapshot_path": "../starting snapshot",
                "initial_snapshot_sha256": hashlib.sha256(json_bytes(initial)).hexdigest(), "initial_tick": 101}
    write_json(source / "manifest.json", manifest)
    return game, save, snapshot, source, manifest


def prepare(tmp_path, **kwargs):
    game, save, snapshot, source, manifest = fixture(tmp_path, **kwargs)
    output = tmp_path / "repeat output"
    plan = repeat.prepare_repeat(source, model="new-model", output_dir=output, game_dir=game, game_stopped=True)
    return plan, output, game, save, snapshot, source, manifest


@pytest.mark.parametrize("provider", ["ollama", "compatible"])
def test_restore_preserves_previous_save_and_freezes_settings(tmp_path, provider):
    plan, output, game, save, snapshot, source, manifest = prepare(tmp_path, provider=provider)
    assert (save / "world.sav").read_bytes() == b"fake starting world"
    backup = repeat._resolve(plan["restore_receipt"]["backup_directory"], output)
    assert backup.parent == game / "dfhack-config" / "dfeval-backups"
    assert (backup / "world.sav").read_bytes() == b"fake post-run world to preserve"
    expected = {key: manifest["config"][key] for key in repeat._CONFIG_KEYS}
    expected["stop_file"] = None
    assert plan["config"] == expected
    changed = copy.deepcopy(manifest["policy"])
    changed["model"] = "new-model"
    if provider == "ollama":
        changed["model_identity"] = None
    assert plan["policy_config"] == changed
    assert repeat.policy_from_plan(plan).public_config() == changed
    assert plan["source"]["stop_file_relocated"] is True
    assert plan["resolved_paths"]["run_dir"] == str(output / "run")
    assert not (output / "run").exists()
    assert set(path.name for path in (output / "source").iterdir()) == {"manifest.json", "initial.json"}
    assert str(tmp_path) not in json.dumps(plan["paths"])


@pytest.mark.parametrize("provider", ["ollama", "compatible", "cloud"])
@pytest.mark.parametrize("system_prompt", [SYSTEM_PROMPT, LEGACY_SYSTEM_PROMPT], ids=["current", "legacy"])
def test_repeat_transmits_source_briefing_even_when_default_changed(tmp_path, monkeypatch, provider, system_prompt):
    monkeypatch.setenv("DFEVAL_REPEAT_TEST_KEY", "local-test-value")
    plan, output, *_ = prepare(tmp_path, provider=provider, system_prompt=system_prompt)
    loaded = repeat.load_repeat(output)
    assert loaded["policy_config"]["system_prompt"] == system_prompt
    policy = repeat.policy_from_plan(loaded)
    decision = {"action": "wait", "reason": "Observe the consequences.",
                "notebook": "My interpretation of care remains open to revision."}
    message = {"role": "assistant", "content": json.dumps(decision)}
    # Both provider envelopes are supplied by this in-memory fake; no model or
    # HTTP server runs, and the restored files are temporary fake game saves.
    raw = json_bytes({"message": message, "done": True, "done_reason": "stop",
                      "choices": [{"finish_reason": "stop", "message": message}]})
    requests = []

    class Opener:
        def open(self, req, timeout):
            requests.append(json.loads(req.data))
            return io.BytesIO(raw)

    policy._opener = Opener()
    assert policy.choose({}, []) == decision
    assert len(requests) == 1
    assert requests[0]["messages"][0] == {"role": "system", "content": system_prompt}
    assert policy.last_exchange["request"] == requests[0]
    assert policy.public_config() == plan["policy_config"]


def test_model_failure_is_a_valid_source_outcome(tmp_path):
    plan, *_ = prepare(tmp_path, outcome="error")
    assert plan["source"]["outcome"] == "error"
    assert any("Terminal outcome is error" in warning for warning in plan["source"]["warnings"])


@pytest.mark.parametrize("control,policy_type", [("idle", IdlePolicy), ("rule", RulePolicy)])
@pytest.mark.parametrize("provider", ["ollama", "compatible", "cloud"])
def test_control_repeat_preserves_source_budgets_start_and_backup_without_provider_calls(
        tmp_path, monkeypatch, control, policy_type, provider):
    game, save, snapshot, source, manifest = fixture(tmp_path, provider=provider)
    output = tmp_path / "control repeat"
    monkeypatch.delenv("DFEVAL_REPEAT_TEST_KEY", raising=False)

    def no_provider(*args, **kwargs):
        pytest.fail("A scripted control must not contact or execute a model provider")

    monkeypatch.setattr(request.OpenerDirector, "open", no_provider)
    monkeypatch.setattr(OllamaPolicy, "choose", no_provider)
    monkeypatch.setattr(ChatCompletionsPolicy, "choose", no_provider)
    plan = repeat.prepare_repeat(source, control=control, output_dir=output,
                                 game_dir=game, game_stopped=True)
    assert plan["kind"] == "native_control_repeat"
    assert plan["model"] is None
    assert plan["policy_config"] == policy_type().public_config()
    assert plan["source"]["original_model"] == "original-model"
    expected = {key: manifest["config"][key] for key in repeat._CONFIG_KEYS}
    expected["stop_file"] = None
    assert plan["config"] == expected
    initial = json.loads((output / "source" / "initial.json").read_text())
    assert plan["initial_expectation"] == {
        "initial_observation_sha256": comparison.initial_observation_fingerprint(initial),
        "save_directory": "region1", "df_version": "53.16", "dfhack_version": "53.16-r1.1",
        "protocol": manifest["protocol"], "bridge_sha256": manifest["bridge_sha256"],
        "game_environment_sha256": manifest["game_environment"]["fingerprint_sha256"],
    }
    assert plan["config"]["starting_save_sha256"] == hashlib.sha256((snapshot / "manifest.json").read_bytes()).hexdigest()
    assert (save / "world.sav").read_bytes() == b"fake starting world"
    backup = repeat._resolve(plan["restore_receipt"]["backup_directory"], output)
    assert (backup / "world.sav").read_bytes() == b"fake post-run world to preserve"
    loaded = repeat.load_repeat(output)
    assert loaded == plan
    policy = repeat.policy_from_plan(loaded)
    assert isinstance(policy, policy_type)
    assert policy.public_config() == plan["policy_config"]
    decision = policy.choose({"workshops": [{"id": 9, "type": "Still", "completed": True, "jobs": []}]}, [])
    assert decision["action"] == ("brew" if control == "rule" else "wait")
    assert not (output / "run").exists()


@pytest.mark.parametrize("selection,match", [
    ({}, "exactly one"),
    ({"model": "new-model", "control": "idle"}, "exactly one"),
    ({"model": "new-model", "control": "rule"}, "exactly one"),
    ({"control": "unknown"}, "idle or rule"),
])
def test_model_and_control_selection_rejected_before_restore(tmp_path, monkeypatch, selection, match):
    game, save, _, source, _ = fixture(tmp_path)
    monkeypatch.setattr(scenario, "restore_save", lambda *a, **kw: pytest.fail("Invalid selection reached restore"))
    output = tmp_path / "repeat"
    with pytest.raises(ValueError, match=match):
        repeat.prepare_repeat(source, **selection, output_dir=output, game_dir=game, game_stopped=True)
    assert not output.exists()
    assert (save / "world.sav").read_bytes() == b"fake post-run world to preserve"


@pytest.mark.parametrize("control", ["idle", "rule"])
@pytest.mark.parametrize("change,match", [
    ("model", "cannot select a model"),
    ("is_model", "Control settings"),
    ("extra_setting", "Control settings"),
    ("observation_version", "Control settings"),
    ("unknown_kind", "idle or rule"),
    ("budget", "settings differ"),
])
def test_control_recipe_rejects_resealed_changed_baseline_or_budget(tmp_path, control, change, match):
    game, _, _, source, _ = fixture(tmp_path)
    output = tmp_path / "control"
    repeat.prepare_repeat(source, control=control, output_dir=output, game_dir=game, game_stopped=True)
    plan = json.loads((output / "repeat.json").read_text())
    if change == "model":
        plan["model"] = "smuggled-model"
    elif change == "is_model":
        plan["policy_config"]["is_model"] = True
    elif change == "extra_setting":
        plan["policy_config"]["temperature"] = 0
    elif change == "observation_version":
        plan["policy_config"]["model_observation_version"] = "native-care-v999"
    elif change == "unknown_kind":
        plan["policy_config"]["kind"] = "custom"
    else:
        plan["config"]["ticks_per_decision"] += 1
    plan["seal_sha256"] = repeat._seal(plan)
    write_json(output / "repeat.json", plan)
    with pytest.raises(ValueError, match=match):
        repeat.load_repeat(output)
    if change != "budget":
        with pytest.raises(ValueError, match=match):
            repeat.policy_from_plan(plan)


def test_rule_recipe_rejects_resealed_changed_rule_description(tmp_path):
    game, _, _, source, _ = fixture(tmp_path)
    output = tmp_path / "rule control"
    repeat.prepare_repeat(source, control="rule", output_dir=output, game_dir=game, game_stopped=True)
    plan = json.loads((output / "repeat.json").read_text())
    plan["policy_config"]["rule"] = "queue ten jobs regardless of workshop state"
    plan["seal_sha256"] = repeat._seal(plan)
    write_json(output / "repeat.json", plan)
    with pytest.raises(ValueError, match="Control settings"):
        repeat.load_repeat(output)


@pytest.mark.parametrize("control", ["idle", "rule"])
def test_control_preserves_explicit_operator_stop_file(tmp_path, control):
    stop = tmp_path / "operator-stop"
    game, _, _, source, _ = fixture(tmp_path, stop=stop)
    plan = repeat.prepare_repeat(source, control=control, output_dir=tmp_path / "repeat",
                                 game_dir=game, game_stopped=True)
    assert plan["config"]["stop_file"] == str(stop)
    assert plan["source"]["stop_file_relocated"] is False


@pytest.mark.parametrize("selection", [{"model": "new-model"}, {"control": "idle"}])
@pytest.mark.parametrize("change", ["manifest_version", "config_version", "policy_version",
                                   "missing_contract", "changed_contract", "unversioned"])
def test_repeat_requires_current_source_observation_metadata_before_restore(
        tmp_path, monkeypatch, selection, change):
    game, save, _, source, manifest = fixture(tmp_path)
    if change == "manifest_version":
        manifest["model_observation_version"] = "native-care-v999"
    elif change == "config_version":
        manifest["config"].pop("model_observation_version")
    elif change == "policy_version":
        manifest["policy"]["model_observation_version"] = "native-care-v999"
        manifest["config"]["policy"] = copy.deepcopy(manifest["policy"])
    elif change == "missing_contract":
        manifest.pop("model_observation")
    elif change == "changed_contract":
        manifest["model_observation"]["max_observation_bytes"] += 1
    else:
        manifest.pop("model_observation")
        for container in (manifest, manifest["config"], manifest["policy"], manifest["config"]["policy"]):
            container.pop("model_observation_version", None)
    write_json(source / "manifest.json", manifest)
    events = [json.loads(line) for line in (source / "events.jsonl").read_text().splitlines()]
    events[0]["config"] = manifest["config"]
    (source / "events.jsonl").write_bytes(b"".join(json_bytes(event) + b"\n" for event in events))
    monkeypatch.setattr(scenario, "restore_save", lambda *a, **kw: pytest.fail("Old or inconsistent metadata reached restore"))
    output = tmp_path / "repeat"
    with pytest.raises(ValueError, match="observation|current ExperimentConfig"):
        repeat.prepare_repeat(source, **selection, output_dir=output, game_dir=game, game_stopped=True)
    assert not output.exists()
    assert (save / "world.sav").read_bytes() == b"fake post-run world to preserve"


def test_load_survives_original_run_removal_and_does_not_restore_again(tmp_path, monkeypatch):
    plan, output, game, save, snapshot, source, _ = prepare(tmp_path)
    shutil.rmtree(source)
    (save / "world.sav").write_bytes(b"game may now be running")
    monkeypatch.setattr(scenario, "restore_save", lambda *a, **kw: pytest.fail("load must never restore"))
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: pytest.fail("load must not require a stopped game"))
    loaded = repeat.load_repeat(output)
    assert loaded == plan
    assert (save / "world.sav").read_bytes() == b"game may now be running"


def test_relative_recipe_can_move_with_related_directories(tmp_path):
    parent = tmp_path / "old"
    parent.mkdir()
    plan, output, *_ = prepare(parent)
    moved = tmp_path / "moved"
    parent.rename(moved)
    loaded = repeat.load_repeat(moved / output.name)
    assert loaded["config"]["stop_file"] is None
    assert loaded["resolved_paths"]["game_dir"] == str(moved / "game")
    assert loaded["seal_sha256"] == plan["seal_sha256"]


def test_custom_stop_file_is_preserved(tmp_path):
    stop = tmp_path / "operator-stop"
    plan, *_ = prepare(tmp_path, stop=stop)
    assert plan["config"]["stop_file"] == str(stop)
    assert plan["source"]["stop_file_relocated"] is False


def test_appdata_save_root_and_native_display_version_repeat_round_trip(tmp_path):
    save_root = tmp_path / "AppData" / "Roaming" / "Bay 12 Games" / "Dwarf Fortress" / "save"
    game, saved, snapshot, source, _ = fixture(tmp_path, save_root=save_root,
                                              native_version="v0.53.16 win64 ITCH")
    output = tmp_path / "repeat"
    plan = repeat.prepare_repeat(source, model="different-model", output_dir=output, game_dir=game,
                                 game_stopped=True, save_root=save_root)
    assert (saved / "world.sav").read_bytes() == b"fake starting world"
    assert plan["initial_expectation"]["df_version"] == "v0.53.16 win64 ITCH"
    assert scenario.verify_snapshot(snapshot)["df_version"] == "53.16"
    assert repeat._resolve(plan["paths"]["save_root"], output) == save_root
    assert repeat._resolve(plan["restore_receipt"]["save_root"], output) == save_root
    assert plan["resolved_paths"]["restored_save_dir"] == str(saved)
    assert not (game / "save").exists()
    assert repeat.load_repeat(output) == plan
    assert str(tmp_path) not in json.dumps(plan["paths"])


@pytest.mark.parametrize("change", ["receipt_target", "receipt_root", "recipe_root", "missing_receipt_root"])
def test_external_root_receipt_stays_bound_to_selected_root_even_if_resealed(tmp_path, change):
    save_root = tmp_path / "external saves"
    game, _, _, source, _ = fixture(tmp_path, save_root=save_root)
    output = tmp_path / "repeat"
    repeat.prepare_repeat(source, model="different-model", output_dir=output, game_dir=game,
                          game_stopped=True, save_root=save_root)
    plan = json.loads((output / "repeat.json").read_text())
    other = tmp_path / "other saves"
    other.mkdir()
    if change == "receipt_target":
        plan["restore_receipt"]["save_directory"] = repeat._relative(other / "region1", output)
    elif change == "receipt_root":
        plan["restore_receipt"]["save_root"] = repeat._relative(other, output)
    elif change == "recipe_root":
        plan["paths"]["save_root"] = repeat._relative(other, output)
    else:
        plan["restore_receipt"].pop("save_root")
    plan["seal_sha256"] = repeat._seal(plan)
    write_json(output / "repeat.json", plan)
    with pytest.raises(ValueError, match="save root|save-root"):
        repeat.load_repeat(output)


def test_native_display_version_does_not_admit_different_snapshot_release(tmp_path):
    game, _, _, source, _ = fixture(tmp_path, native_version="v0.53.17 win64 ITCH")
    with pytest.raises(ValueError, match="snapshot game version"):
        repeat.prepare_repeat(source, model="different-model", output_dir=tmp_path / "out",
                              game_dir=game, game_stopped=True)


def test_native_display_version_normalizes_only_game_preflight_not_dfhack(tmp_path, monkeypatch):
    game, _, _, source, _ = fixture(tmp_path, native_version="v0.53.16 win64 ITCH")
    monkeypatch.setattr(repeat, "inspect_installation", lambda _: {
        "game": {"version": "53.16"}, "dfhack": {"version": "53.16-r2"}})
    with pytest.raises(ValueError, match="Installed game or DFHack version"):
        repeat.prepare_repeat(source, model="different-model", output_dir=tmp_path / "out",
                              game_dir=game, game_stopped=True)


def test_explicit_snapshot_and_backup(tmp_path):
    game, save, snapshot, source, manifest = fixture(tmp_path)
    manifest.pop("starting_snapshot_path")
    write_json(source / "manifest.json", manifest)
    backup = tmp_path / "explicit backup"
    plan = repeat.prepare_repeat(source, model="different", output_dir=tmp_path / "out", game_dir=game,
                                 snapshot_dir=snapshot, backup_dir=backup, game_stopped=True)
    assert (backup / "world.sav").read_bytes() == b"fake post-run world to preserve"
    assert plan["restore_receipt"]["integrity_verified"] is True


@pytest.mark.parametrize("change,match", [
    ("no_path", "--starting-snapshot"), ("no_hash", "cannot reconstruct"),
    ("wrong_bridge", "bridge hash"), ("wrong_policy", "model policy"),
    ("wrong_prompt", "system prompt"), ("missing_budget", "settings|budgets"),
    ("extra_budget", "unknown settings"), ("wrong_initial", "snapshot"),
    ("incomplete", "complete coherent"), ("wrong_game", "version"),
])
def test_invalid_source_rejected_before_restore(tmp_path, monkeypatch, change, match):
    game, save, snapshot, source, manifest = fixture(tmp_path)
    events = [json.loads(line) for line in (source / "events.jsonl").read_text().splitlines()]
    if change == "no_path": manifest.pop("starting_snapshot_path")
    elif change == "no_hash":
        manifest["config"]["starting_save_sha256"] = None
        manifest["starting_save_sha256"] = None
    elif change == "wrong_bridge": manifest["bridge_sha256"] = "0" * 64
    elif change == "wrong_policy":
        manifest["policy"] = IdlePolicy().public_config()
        manifest["config"]["policy"] = manifest["policy"]
    elif change == "wrong_prompt": manifest["policy"]["system_prompt"] = "Changed prompt"
    elif change == "missing_budget": manifest["config"].pop("history_decisions")
    elif change == "extra_budget": manifest["config"]["future_setting"] = 1
    elif change == "wrong_initial": manifest["initial_snapshot_sha256"] = "0" * 64
    elif change == "incomplete": events.pop()
    elif change == "wrong_game": (game / "release notes.txt").write_text("Release notes for 53.17\n")
    events[0]["config"] = manifest["config"]
    # JSON-decoded policy copies must remain coherent for tests targeting policy semantics.
    if change == "wrong_prompt":
        manifest["config"]["policy"] = manifest["policy"]
    write_json(source / "manifest.json", manifest)
    (source / "events.jsonl").write_bytes(b"".join(json_bytes(event) + b"\n" for event in events))
    monkeypatch.setattr(scenario, "restore_save", lambda *a, **kw: pytest.fail("invalid source reached restore"))
    with pytest.raises((ValueError, scenario.ScenarioError), match=match):
        repeat.prepare_repeat(source, model="different", output_dir=tmp_path / "out", game_dir=game, game_stopped=True)
    assert not (tmp_path / "out").exists()
    assert (save / "world.sav").read_bytes() == b"fake post-run world to preserve"


@pytest.mark.parametrize("change", ["settings", "policy", "expectation", "path", "initial", "manifest", "snapshot"])
def test_load_rejects_tampered_recipe_or_evidence(tmp_path, change):
    plan, output, game, save, snapshot, source, _ = prepare(tmp_path)
    raw = json.loads((output / "repeat.json").read_text())
    if change == "settings": raw["config"]["max_total_ticks"] += 1
    elif change == "policy": raw["policy_config"]["options"]["seed"] += 1
    elif change == "expectation": raw["initial_expectation"]["save_directory"] = "another"
    elif change == "path": raw["paths"]["run_dir"] = "../outside"
    elif change == "initial": (output / "source" / "initial.json").write_text("{}")
    elif change == "manifest": (output / "source" / "manifest.json").write_text("{}")
    elif change == "snapshot": (snapshot / "save" / "world.sav").write_bytes(b"tampered")
    write_json(output / "repeat.json", raw)
    with pytest.raises((ValueError, scenario.ScenarioError)):
        repeat.load_repeat(output)


def test_resealed_changed_budget_still_disagrees_with_source(tmp_path):
    _, output, *_ = prepare(tmp_path)
    raw = json.loads((output / "repeat.json").read_text())
    raw["config"]["max_total_ticks"] += 1
    raw["seal_sha256"] = repeat._seal(raw)
    write_json(output / "repeat.json", raw)
    with pytest.raises(ValueError, match="settings differ"):
        repeat.load_repeat(output)


def test_requires_stopped_and_new_output(tmp_path):
    game, save, snapshot, source, manifest = fixture(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="--game-stopped"):
        repeat.prepare_repeat(source, model="different", output_dir=out, game_dir=game)
    out.mkdir()
    with pytest.raises(ValueError, match="must be new"):
        repeat.prepare_repeat(source, model="different", output_dir=out, game_dir=game, game_stopped=True)


def test_running_game_blocks_restore_and_unsealed_plan_cannot_run(tmp_path, monkeypatch):
    game, save, snapshot, source, _ = fixture(tmp_path)
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: ["Dwarf Fortress.exe"])
    out = tmp_path / "out"
    with pytest.raises(scenario.ScenarioError, match="still running"):
        repeat.prepare_repeat(source, model="different", output_dir=out, game_dir=game, game_stopped=True)
    with pytest.raises(ValueError, match="preparation did not finish"):
        repeat.load_repeat(out)
    assert (save / "world.sav").read_bytes() == b"fake post-run world to preserve"


def test_excessive_json_depth_is_clean_error(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "repeat.json").write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError, match="nested JSON"):
        repeat.load_repeat(out)


def test_output_cannot_overlap_source_or_game(tmp_path):
    game, save, snapshot, source, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="separate"):
        repeat.prepare_repeat(source, model="different", output_dir=game / "repeat", game_dir=game, game_stopped=True)


def test_cloud_tag_is_rejected_before_restore(tmp_path, monkeypatch):
    game, _, _, source, _ = fixture(tmp_path)
    monkeypatch.setattr(scenario, "restore_save", lambda *a, **kw: pytest.fail("cloud tag reached restore"))
    with pytest.raises(ValueError, match="cloud-tag"):
        repeat.prepare_repeat(source, model="example:cloud", output_dir=tmp_path / "out", game_dir=game, game_stopped=True)


def test_changed_environment_refuses_prepare_before_save_mutation(tmp_path):
    game, save, _, source, _ = fixture(tmp_path)
    (game / "prefs").mkdir()
    (game / "prefs" / "d_init.txt").write_text("[TEMPERATURE:NO]")
    with pytest.raises(ValueError, match="configuration differs"):
        repeat.prepare_repeat(source, model="different", output_dir=tmp_path / "out", game_dir=game, game_stopped=True)
    assert (save / "world.sav").read_bytes() == b"fake post-run world to preserve"
    assert not (tmp_path / "out").exists()


def test_changed_environment_refuses_prepared_repeat(tmp_path):
    _, output, game, *_ = prepare(tmp_path)
    (game / "prefs").mkdir()
    (game / "prefs" / "init.txt").write_text("[FPS_CAP:20]")
    with pytest.raises(ValueError, match="configuration differs"):
        repeat.load_repeat(output)


def test_old_source_cannot_invent_missing_environment(tmp_path):
    game, _, _, source, manifest = fixture(tmp_path)
    manifest.pop("game_environment")
    write_json(source / "manifest.json", manifest)
    with pytest.raises(ValueError, match="no recorded installation environment"):
        repeat.prepare_repeat(source, model="different", output_dir=tmp_path / "out", game_dir=game, game_stopped=True)


def test_repeat_output_link_cannot_redirect_recording(tmp_path):
    _, output, *_ = prepare(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (output / "run").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable on this host")
    with pytest.raises((ValueError, scenario.ScenarioError), match="[Ss]ymlink|reparse"):
        repeat.load_repeat(output)
    assert list(outside.iterdir()) == []


def test_completed_recipe_cannot_overwrite_run_evidence(tmp_path):
    _, output, *_ = prepare(tmp_path)
    (output / "run").mkdir()
    evidence = output / "run" / "events.jsonl"
    evidence.write_bytes(b"keep previous result")
    with pytest.raises(ValueError, match="already has run evidence"):
        repeat.load_repeat(output)
    assert evidence.read_bytes() == b"keep previous result"
