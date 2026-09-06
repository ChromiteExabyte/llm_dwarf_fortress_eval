from dataclasses import asdict
import hashlib
import json

import pytest

from dfeval.comparison import (COMPARISON_FIELDS, _evidence_json, _reported_fps, compare_runs,
                               initial_observation_fingerprint, render_comparison_report)
from dfeval.experiment import ExperimentConfig, run_experiment
from dfeval.live import PROTOCOL_VERSION
from dfeval.model_observation import (MODEL_OBSERVATION_VERSION, model_input_bytes,
                                      project_observation, projection_contract)
from dfeval.policies import DecisionError, IdlePolicy, json_bytes


def snapshot(tick=100, drink=10):
    return {"world_loaded": True, "map_loaded": True, "fortress_mode": True, "paused": True,
            "df_version": "test-df", "dfhack_version": "test-dfhack", "absolute_tick": tick,
            "save_directory": "test-save", "errors": [],
            "citizens": [{"id": 1, "dead": False, "stress": 0, "thirst_timer": 15, "needs": []}],
            "known_former_citizens": [], "stocks": {"by_item_type": {"DRINK": {"stack_units": drink}}}}


def events_at(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


def write_events(root, events):
    (root / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events))


def change_config(root, **updates):
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["config"].update(updates)
    if "policy" in updates:
        manifest["policy"] = updates["policy"]
    (root / "manifest.json").write_text(json.dumps(manifest))
    events = events_at(root)
    events[0]["config"] = manifest["config"]
    write_events(root, events)


def record(root, *, save="a" * 64, final_drink=12, complete=True, initial_tick=100):
    root.mkdir()
    policy = {"kind": "rule", "is_model": False}
    limits = {**asdict(ExperimentConfig(starting_save_sha256=save)), "policy": policy,
              "scenario": "drink-maintenance-v1"}
    initial = snapshot(initial_tick, 10)
    manifest = {"schema_version": 1, "protocol": PROTOCOL_VERSION, "bridge_sha256": "b" * 64,
                "interface": "native measurements + wait/brew/finish", "events_file": "events.jsonl",
                "config": limits, "policy": policy, "starting_save_sha256": save,
                "initial_snapshot_sha256": hashlib.sha256(json_bytes(initial)).hexdigest(),
                "initial_tick": initial_tick, "state": "finished" if complete else "running"}
    (root / "manifest.json").write_text(json.dumps(manifest))
    events = [{"kind": "run_start", "config": limits}, {"kind": "snapshot", "snapshot": initial},
              {"kind": "snapshot", "snapshot": snapshot(initial_tick + 1200, final_drink)}]
    if complete:
        events.append({"kind": "run_end", "outcome": "finished", "pause_confirmed": True,
                       "summary": {"drink_stack_units_change": 999999}})
    for index, event in enumerate(events):
        event.update(event=index, at="2026-09-05T12:00:00+00:00", wall_seconds=float(index), turn=0)
    write_events(root, events)
    # Stored scores are deliberately incorrect; evidence is the basis of comparison.
    (root / "result.json").write_text('{"summary":{"population_change":999999}}')
    return root


def test_comparison_recomputes_outcomes_and_matches_recorded_setup(tmp_path):
    first = record(tmp_path / "first")
    second = record(tmp_path / "second", final_drink=15)
    report = compare_runs([first, second])
    assert report["recorded_setup_matches"] is True
    assert [run["summary"]["drink_stack_units_change"] for run in report["runs"]] == [2, 5]
    assert report["runs"][0]["summary"]["population_change"] == 0
    assert "999999" not in render_comparison_report(report)


def versioned_record(root):
    record(root)
    manifest = json.loads((root / "manifest.json").read_text())
    for container in (manifest, manifest["config"], manifest["policy"], manifest["config"]["policy"]):
        container["model_observation_version"] = MODEL_OBSERVATION_VERSION
    manifest["model_observation"] = projection_contract()
    (root / "manifest.json").write_text(json.dumps(manifest))
    events = events_at(root)
    events[0]["config"] = manifest["config"]
    seen = project_observation(events[1]["snapshot"])
    body = model_input_bytes(seen, [])
    events.insert(2, {"kind": "policy_input", "observation": seen, "history": [],
                     "model_observation_version": MODEL_OBSERVATION_VERSION,
                     "input_bytes": len(body), "input_sha256": hashlib.sha256(body).hexdigest()})
    for index, event in enumerate(events):
        event.update(event=index, at="2026-09-05T12:00:00+00:00", wall_seconds=float(index), turn=0)
    write_events(root, events)
    return root


def test_versioned_inputs_are_checked_separately_from_historical_native_start(tmp_path):
    legacy = record(tmp_path / "legacy")
    current = versioned_record(tmp_path / "current")
    report = compare_runs([legacy, current])
    assert not report["recorded_setup_matches"]
    assert report["runs"][0]["initial_observation_sha256"] == report["runs"][1]["initial_observation_sha256"]
    assert report["runs"][0]["model_observation_version"] is None
    assert report["runs"][0]["model_input_contract_verified"] is None
    assert report["runs"][1]["model_input_contract_verified"] is True
    assert all("model observation input contract" in issue for issue in report["comparability_issues"])


def test_projected_input_tampering_cannot_be_hidden_by_a_matching_input_hash(tmp_path):
    first, second = [versioned_record(tmp_path / name) for name in ("first", "second")]
    assert compare_runs([first, second])["recorded_setup_matches"]
    events = events_at(second)
    seen = next(event for event in events if event["kind"] == "policy_input")
    seen["observation"]["citizens"][0]["stress"] = 999
    body = model_input_bytes(seen["observation"], seen["history"])
    seen.update(input_bytes=len(body), input_sha256=hashlib.sha256(body).hexdigest())
    write_events(second, events)
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert report["runs"][1]["model_input_contract_verified"] is False
    assert any("preceding native snapshot" in issue for issue in report["comparability_issues"])
    assert report["runs"][1]["summary"]["initial"]["citizen_measurements"]["stress"]["mean"] == 0


def test_model_input_version_declarations_must_agree(tmp_path):
    first, second = [versioned_record(tmp_path / name) for name in ("first", "second")]
    manifest = json.loads((second / "manifest.json").read_text())
    manifest["model_observation_version"] = "native-care-v2"
    (second / "manifest.json").write_text(json.dumps(manifest))
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert any("Model observation version" in issue for issue in report["comparability_issues"])


def test_missing_provenance_is_a_comparison_limit(tmp_path):
    report = compare_runs([record(tmp_path / "a", save=None), record(tmp_path / "b", save=None)])
    assert not report["recorded_setup_matches"]
    assert any("no declared starting-save" in issue for issue in report["comparability_issues"])


def test_different_initial_state_and_limits_are_reported(tmp_path):
    first = record(tmp_path / "a")
    second = record(tmp_path / "b", initial_tick=101, save="b" * 64)
    manifest = json.loads((second / "manifest.json").read_text())
    manifest["config"]["ticks_per_decision"] = 600
    (second / "manifest.json").write_text(json.dumps(manifest))
    report = compare_runs([first, second])
    issues = "\n".join(report["comparability_issues"])
    assert not report["recorded_setup_matches"]
    assert "initial native observation" in issues
    assert "starting-save identity" in issues
    assert "budgets and timing" in issues


def test_partial_tail_is_disclosed_and_not_treated_as_complete(tmp_path):
    first = record(tmp_path / "a")
    second = record(tmp_path / "b", complete=False)
    with (second / "events.jsonl").open("a") as stream:
        stream.write('{"kind":')
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert not report["runs"][1]["complete_record"]
    assert "Incomplete final log" in "\n".join(report["comparability_issues"])


def test_invalid_complete_event_is_rejected(tmp_path):
    first = record(tmp_path / "a")
    second = record(tmp_path / "b")
    with (second / "events.jsonl").open("a") as stream:
        stream.write('{"bad":NaN}\n')
    with pytest.raises(ValueError, match="Malformed complete event"):
        compare_runs([first, second])


def test_native_comparison_rejects_mock_only_records(tmp_path):
    first = record(tmp_path / "a")
    second = record(tmp_path / "b")
    (second / "events.jsonl").write_text('{"kind":"run_end","data":{"bridge":"mock"}}\n')
    with pytest.raises(ValueError, match="no native experiment snapshots"):
        compare_runs([first, second])


@pytest.mark.parametrize("field", COMPARISON_FIELDS)
def test_every_runner_budget_affects_recorded_setup(tmp_path, field):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    config = json.loads((second / "manifest.json").read_text())["config"]
    change_config(second, **{field: config[field] + 1})
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert any("budgets and timing" in issue for issue in report["comparability_issues"])
    assert report["runs"][1]["warnings"] == []


def test_positive_fractional_time_limits_are_valid(tmp_path):
    paths = [record(tmp_path / name) for name in ("a", "b")]
    for root in paths:
        change_config(root, request_timeout=0.5, max_wall_seconds=0.75)
    assert compare_runs(paths)["recorded_setup_matches"]


@pytest.mark.parametrize("value,expected", [
    (1, 1), (1.0, 1), (1000, 1000), (1000.0, 1000), (10000.0, 10000),
    (0, None), (-1.0, None), (10000.1, None), (10001, None), (999.5, None),
    (True, None), (False, None), (float("nan"), None), (float("inf"), None),
    (float("-inf"), None), ("1000.0", None), (None, None),
])
def test_reported_native_fps_accepts_only_finite_integral_values_in_range(value, expected):
    assert _reported_fps(value) == expected


def test_actual_native_integral_float_fps_reports_without_changing_guard_hash(tmp_path):
    paths = [record(tmp_path / name) for name in ("a", "b")]
    fingerprints = []
    for path in paths:
        events = events_at(path)
        for event in events:
            if event["kind"] == "snapshot":
                event["snapshot"]["simulation_fps"] = {"effective": 1000.0, "graphics_cap": 50.0}
        initial = events[1]["snapshot"]
        fingerprints.append(initial_observation_fingerprint(initial))
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["initial_snapshot_sha256"] = hashlib.sha256(json_bytes(initial)).hexdigest()
        (path / "manifest.json").write_text(json.dumps(manifest))
        write_events(path, events)
    report = compare_runs(paths)
    assert report["recorded_setup_matches"]
    assert [run["limits"]["effective_simulation_fps"] for run in report["runs"]] == [1000, 1000]
    assert [run["initial_observation_sha256"] for run in report["runs"]] == fingerprints
    stored = events_at(paths[0])[1]["snapshot"]["simulation_fps"]["effective"]
    assert type(stored) is float and stored == 1000.0


@pytest.mark.parametrize(("field", "value", "message"), [
    ("protocol", 999, "protocol identity"),
    ("bridge_sha256", "c" * 64, "script/protocol identity"),
    ("bridge_sha256", None, "Bridge script SHA-256"),
    ("initial_snapshot_sha256", "c" * 64, "does not match the hash"),
    ("events_file", None, "provenance"),
    ("events", 9000, "event count"),
    ("event_bytes", 9000, "byte count"),
    ("outcome", "error", "outcome disagrees"),
])
def test_manifest_integrity_or_identity_differences_are_explicit(tmp_path, field, value, message):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    manifest = json.loads((second / "manifest.json").read_text())
    manifest[field] = value
    (second / "manifest.json").write_text(json.dumps(manifest))
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert message in "\n".join(report["comparability_issues"])
    assert report["runs"][1]["summary"]["drink_stack_units_change"] == 2


def test_policy_identity_and_per_call_tokens_are_information_not_setup_mismatch(tmp_path):
    first, second = record(tmp_path / "baseline"), record(tmp_path / "model")
    model = {"kind": "chat_completions", "is_model": True, "model": "test-model",
             "max_completion_tokens": 256, "token_limit_field": "max_tokens"}
    change_config(second, policy=model)
    report = compare_runs([first, second])
    assert report["recorded_setup_matches"]
    assert report["runs"][1]["policy_config"] == model
    assert "response tokens 256; field max_tokens" in render_comparison_report(report)


@pytest.mark.parametrize("change", ["duplicate_id", "missing_start", "after_end", "duplicate_end",
                                    "backwards_wall", "backwards_turn", "missing_timestamp"])
def test_incoherent_stream_is_retained_with_warnings(tmp_path, change):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    events = events_at(second)
    if change == "duplicate_id":
        events[2]["event"] = events[1]["event"]
    elif change == "missing_start":
        events[0]["kind"] = "bootstrap"
    elif change == "after_end":
        events.append({**events[2], "event": 4, "wall_seconds": 4})
    elif change == "duplicate_end":
        events.append({**events[-1], "event": 4, "wall_seconds": 4})
    elif change == "backwards_wall":
        events[2]["wall_seconds"] = 0
    elif change == "backwards_turn":
        events[1]["turn"] = 5
    else:
        del events[1]["at"]
    write_events(second, events)
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert not report["runs"][1]["complete_record"]
    assert report["runs"][1]["summary"]["final"]["drink"]["stack_units"] == 12


@pytest.mark.parametrize(("outcome", "pause"), [("error", True), ("cancelled", True),
                                               ("finished", False), ("finished", None)])
def test_terminal_failure_and_unconfirmed_pause_qualify_outcomes(tmp_path, outcome, pause):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    events = events_at(second)
    events[-1].update(outcome=outcome, pause_confirmed=pause)
    write_events(second, events)
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert report["runs"][1]["outcome"] == outcome
    assert report["runs"][1]["pause_confirmed"] is pause
    assert f"Terminal outcome: {outcome}" in render_comparison_report(report)


@pytest.mark.parametrize(("field", "value"), [("absolute_tick", 90), ("save_directory", "other-world"),
                                             ("save_directory", None), ("world_loaded", False),
                                             ("df_version", "changed-version")])
def test_broken_world_or_time_continuity_suppresses_cross_sample_changes(tmp_path, field, value):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    events = events_at(second)
    events[2]["snapshot"][field] = value
    write_events(second, events)
    report = compare_runs([first, second])
    run = report["runs"][1]
    assert not report["recorded_setup_matches"]
    assert not run["world_time_continuity_verified"]
    assert run["summary"]["observed_tick_span"] is None
    assert run["summary"]["population_change"] is None
    assert run["summary"]["drink_stack_units_change"] is None
    assert run["summary"]["final"]["drink"]["stack_units"] == 12


@pytest.mark.parametrize(("field", "value"), [("citizens", [None]), ("citizens", [{"id": []}]),
                                             ("stocks", None), ("errors", ["unit read failed"]),
                                             ("paused", False)])
def test_unavailable_or_malformed_native_measurements_warn_without_crashing(tmp_path, field, value):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    events = events_at(second)
    events[2]["snapshot"][field] = value
    write_events(second, events)
    report = compare_runs([first, second])
    assert not report["recorded_setup_matches"]
    assert report["runs"][1]["warnings"]
    assert render_comparison_report(report)


def test_mixed_native_and_other_snapshot_schema_is_rejected(tmp_path):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    events = events_at(second)
    events[2]["snapshot"] = {"dwarves": [], "tick": 0}
    write_events(second, events)
    with pytest.raises(ValueError, match="malformed or non-native snapshot schema"):
        compare_runs([first, second])


def test_declared_mock_is_rejected_even_with_native_shaped_records(tmp_path):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    events = events_at(second)
    events[0]["backend"] = "mock"
    write_events(second, events)
    with pytest.raises(ValueError, match="declares a mock backend"):
        compare_runs([first, second])


def test_duplicate_directory_is_not_two_runs(tmp_path):
    root = record(tmp_path / "a")
    with pytest.raises(ValueError, match="distinct run directories"):
        compare_runs([root, root / "."])


@pytest.mark.parametrize("filename", ["manifest.json", "events.jsonl"])
@pytest.mark.parametrize("depth", [64, 2000])
def test_excessively_nested_json_is_a_clean_evidence_error(tmp_path, filename, depth):
    first, second = record(tmp_path / "a"), record(tmp_path / "b")
    (second / filename).write_text('{"nested":' + '[' * depth + '0' + ']' * depth + '}\n')
    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        compare_runs([first, second])


def test_evidence_json_accepts_the_inspection_depth_boundary():
    value = _evidence_json("[" * 64 + "0" + "]" * 64)
    for _ in range(64):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == 0


def test_wrapped_decoder_recursion_keeps_the_evidence_depth_diagnostic(monkeypatch):
    def limited_decoder(text):
        try:
            raise RecursionError("decoder recursion limit")
        except RecursionError as exc:
            raise DecisionError("invalid JSON") from exc

    monkeypatch.setattr("dfeval.comparison.strict_json", limited_decoder)
    with pytest.raises(ValueError, match="JSON nesting exceeds"):
        _evidence_json("[]")


def test_real_runner_output_contract_is_comparable_with_injected_offline_bridge(tmp_path):
    class OfflineBridge:
        def __init__(self, game_dir, timeout):
            self.game_dir, self.timeout, self.tick = game_dir, timeout, 100
            self.session = "offline-comparison-test"

        def install_script(self):
            script = self.game_dir / "test-only.lua"
            script.write_text("-- offline test fixture, never executed")
            return script

        def status(self):
            return snapshot(self.tick)

        def observe(self):
            return snapshot(self.tick)

        def pause(self):
            return {"paused": True}

        def advance_ticks(self, ticks, timeout):
            start = self.tick
            self.tick += ticks
            return {"paused": True, "elapsed_ticks": ticks, "requested_ticks": ticks,
                    "start_absolute_tick": start, "absolute_tick": self.tick}

    game = tmp_path / "game"
    game.mkdir()
    paths = [tmp_path / name for name in ("a", "b")]
    for path in paths:
        result = run_experiment(game, IdlePolicy(), output_dir=path,
            config=ExperimentConfig(max_decisions=1, ticks_per_decision=10, max_total_ticks=10,
                                    starting_save_sha256="a" * 64), bridge_factory=OfflineBridge,
            bootstrap=lambda bridge, timeout: {"returncode": 0, "stdout": "offline", "stderr": ""})
        assert result["ok"]
    report = compare_runs(paths)
    assert report["recorded_setup_matches"], report["comparability_issues"]
    assert report["runs"][0]["outcome"] == "budget_exhausted"
    assert report["runs"][0]["summary"]["observed_tick_span"] == 10
