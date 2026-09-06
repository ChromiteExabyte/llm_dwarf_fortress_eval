"""Native-state reference guarding with injected bridges, never a real game."""

import copy
import hashlib
import json

import pytest

from dfeval.comparison import (compare_runs, initial_observation_fingerprint, read_run,
                               validate_initial_expectation)
from dfeval.experiment import ExperimentConfig, run_experiment
from dfeval.live import PROTOCOL_VERSION
from dfeval.policies import IdlePolicy
from test_experiment import Bridge, snapshot


def expectation(sample=None):
    sample = sample or snapshot()
    return {"initial_observation_sha256": initial_observation_fingerprint(sample),
            "save_directory": sample["save_directory"], "df_version": sample["df_version"],
            "dfhack_version": sample["dfhack_version"], "protocol": PROTOCOL_VERSION,
            "bridge_sha256": hashlib.sha256(b"-- test fixture; never executed").hexdigest()}


class TrackingPolicy(IdlePolicy):
    def __init__(self):
        self.calls = 0
    def choose(self, observation, history):
        self.calls += 1
        return super().choose(observation, history)


def run(root, expected, *, bridge=None, config=None):
    game = root / "game"
    game.mkdir(parents=True, exist_ok=True)
    bridge = bridge or Bridge(game, 1)
    policy = TrackingPolicy()
    result = run_experiment(game, policy, initial_expectation=expected,
        config=config or ExperimentConfig(max_decisions=1, ticks_per_decision=10, starting_save_sha256="a" * 64),
        output_dir=root / "run", bridge_factory=lambda *a, **kw: bridge,
        bootstrap=lambda *a: {"returncode": 0})
    events = [json.loads(line) for line in (root / "run" / "events.jsonl").read_text().splitlines()]
    manifest = json.loads((root / "run" / "manifest.json").read_text())
    return result, events, manifest, policy, bridge


def test_matching_reference_runs_and_records_recomputable_guard_everywhere(tmp_path):
    expected = expectation()
    result, events, manifest, policy, bridge = run(tmp_path, expected)
    assert result["ok"] and policy.calls == 1
    check = next(event for event in events if event["kind"] == "initial_state_check")
    assert check["matched"] is True and check["differences"] == []
    assert check["actual"] == expected
    assert check["event"] < next(event["event"] for event in events if event["kind"] == "policy_input")
    assert events[-1]["initial_state_check"] == manifest["initial_state_check"] == result["initial_state_check"]
    assert events[0]["initial_expectation"] == result["initial_expectation"] == expected
    assert bridge.calls[-1][0] == "pause"
    validated = read_run(tmp_path / "run")
    assert validated["initial_state_check"]["matched"] is True
    assert not any("initial-state" in warning.lower() for warning in validated["warnings"])


@pytest.mark.parametrize("field,value", [
    ("initial_observation_sha256", "c" * 64), ("save_directory", "other-save"),
    ("df_version", "different-df"), ("dfhack_version", "different-dfhack"),
    ("bridge_sha256", "b" * 64), ("protocol", PROTOCOL_VERSION + 1),
    ("game_environment_sha256", "d" * 64),
])
def test_each_reference_mismatch_prevents_policy_brew_and_advance(tmp_path, field, value):
    expected = expectation()
    expected[field] = value
    result, events, manifest, policy, bridge = run(tmp_path, expected)
    assert result["ok"] is False and result["outcome"] == "error"
    assert result["pause_confirmed"] is True
    assert policy.calls == result["budgets"]["policy_calls"] == 0
    assert result["budgets"]["requested_ticks"] == 0
    assert not any(operation in ("queue_brew", "advance_ticks") for operation, _ in bridge.calls)
    assert not any(event["kind"] in ("policy_input", "policy_response", "decision") for event in events)
    assert sum(event["kind"] == "snapshot" for event in events) == 1
    assert result["initial_state_check"]["matched"] is False
    assert result["initial_state_check"]["differences"][0]["field"] == field
    assert manifest["initial_state_check"] == result["initial_state_check"]
    verified = read_run(tmp_path / "run")
    assert any("does not match the declared reference" in warning for warning in verified["warnings"])


def test_mismatch_restores_temporary_cap_and_final_pause(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    bridge = Bridge(game, 1)
    speed = []
    def set_fps(fps):
        speed.append(("set", fps))
        return {"simulation_fps": {"effective": fps}}
    def restore():
        speed.append(("restore", 100))
        return {"restored": True}
    bridge.set_simulation_fps, bridge.restore_simulation_fps = set_fps, restore
    expected = expectation()
    expected["initial_observation_sha256"] = "c" * 64
    result, events, _, policy, _ = run(tmp_path, expected, bridge=bridge,
        config=ExperimentConfig(max_decisions=1, simulation_fps=1000))
    assert result["pause_confirmed"] and result["speed_restored"]
    assert result["outcome"] == "error" and policy.calls == 0
    assert speed == [("set", 1000), ("restore", 100)]
    assert any(event["kind"] == "initial_state_check" for event in events)


@pytest.mark.parametrize("field,value", [
    ("initial_observation_sha256", "not-a-hash"), ("bridge_sha256", None),
    ("save_directory", ""), ("save_directory", "x" * 257), ("df_version", "\nunsafe"),
    ("df_version", 53.16), ("dfhack_version", float("nan")), ("dfhack_version", "\ud800"),
    ("protocol", True), ("protocol", 0), ("protocol", 1.5),
])
def test_invalid_expectation_rejected_before_filesystem_game_or_policy_calls(tmp_path, field, value):
    expected = expectation()
    expected[field] = value
    def forbidden(*a, **kw):
        pytest.fail("Invalid expectation must be rejected before any bridge or bootstrap call")
    with pytest.raises(ValueError, match="initial_expectation"):
        run_experiment(tmp_path, TrackingPolicy(), initial_expectation=expected,
                       output_dir=tmp_path / "out", bridge_factory=forbidden, bootstrap=forbidden)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("mutation", ["extra", "missing", "not_object"])
def test_guard_shape_strict_and_does_not_accept_paths_or_provenance(tmp_path, mutation):
    expected = expectation()
    if mutation == "extra":
        expected["reference_path"] = "should-never-be-opened"
    elif mutation == "missing":
        expected.pop("protocol")
    else:
        expected = []
    with pytest.raises(ValueError, match="native identity fields"):
        run_experiment(tmp_path, TrackingPolicy(), initial_expectation=expected)


def test_hashes_are_normalized_without_mutating_caller_expectation():
    expected = expectation()
    expected["bridge_sha256"] = expected["bridge_sha256"].upper()
    cleaned = validate_initial_expectation(expected)
    assert cleaned["bridge_sha256"] == expected["bridge_sha256"].lower()
    assert expected["bridge_sha256"] != cleaned["bridge_sha256"]


def observed_session():
    sample = snapshot()
    sample.update(ui_focus="dwarfmode", paused=True,
                  known_former_citizens=[],
                  brewing={"available": True, "session": "first", "epoch": 1, "events": [],
                           "event_count": 0, "dropped_events": 0, "error_count": 0,
                           "last_error": None, "queued_jobs": 0, "notes": "tracking",
                           "source": "dfhack.eventful.onReactionComplete", "max_events": 4096},
                  simulation_fps={"effective": 1000, "graphics_cap": 50, "original": 100,
                                  "requested": 1000, "original_graphics_cap": 50,
                                  "override_active": True, "restore_error": None,
                                  "capture_error": None, "restore_reason": None, "max_requested": 10000})
    return sample


def test_session_tracking_ui_and_override_bookkeeping_do_not_change_fingerprint():
    a = observed_session()
    b = copy.deepcopy(a)
    b.update(ui_focus="different", paused=False, known_former_citizens=[{"id": 100, "dead": True}])
    b["brewing"].update(session="second", epoch=2, events=[{"id": 3}], event_count=1,
                        dropped_events=1, error_count=1, last_error="old error", queued_jobs=9)
    b["simulation_fps"].update(original=1000, requested=None, override_active=False, restore_reason="old session")
    assert initial_observation_fingerprint(a) == initial_observation_fingerprint(b)
    assert a["brewing"]["session"] == "first"


@pytest.mark.parametrize("field", ["tick", "stress", "need", "stock_item", "job", "workshop", "effective_fps", "graphics", "hook_availability", "future_field"])
def test_relevant_native_state_changes_change_fingerprint(field):
    a = observed_session()
    b = copy.deepcopy(a)
    if field == "tick":
        b["absolute_tick"] += 1
    elif field == "stress":
        b["citizens"][0]["stress"] = 50
    elif field == "need":
        b["citizens"][0]["needs"] = [{"id": 1, "focus_level": -20}]
    elif field == "stock_item":
        b["stocks"]["items"] = [{"id": 3, "in_job": True}]
    elif field == "job":
        b["jobs"] = [{"id": 9, "completion_timer": 30}]
    elif field == "workshop":
        b["workshops"][0]["completed"] = False
    elif field == "effective_fps":
        b["simulation_fps"]["effective"] = 200
    elif field == "graphics":
        b["simulation_fps"]["graphics_cap"] = 20
    elif field == "hook_availability":
        b["brewing"]["available"] = False
    else:
        b["future_native_field"] = 1
    assert initial_observation_fingerprint(a) != initial_observation_fingerprint(b)


def test_legacy_empty_tracking_fingerprint_and_exact_integrity_hash_unchanged(tmp_path):
    sample = snapshot()
    canonical = {key: value for key, value in sample.items() if key not in ("paused", "ui_focus")}
    legacy = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True,
                                      allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    assert initial_observation_fingerprint(sample) == legacy
    _, events, manifest, _, _ = run(tmp_path, expectation())
    initial = next(event["snapshot"] for event in events if event["kind"] == "snapshot")
    from dfeval.policies import json_bytes
    assert manifest["initial_snapshot_sha256"] == hashlib.sha256(json_bytes(initial)).hexdigest()


def test_comparison_cannot_claim_match_when_guard_log_is_forged_or_missing(tmp_path):
    run(tmp_path / "first", expectation())
    run(tmp_path / "second", expectation())
    log = tmp_path / "second" / "run" / "events.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    check = next(event for event in events if event["kind"] == "initial_state_check")
    check["actual"]["save_directory"] = "forged"
    log.write_text("".join(json.dumps(event) + "\n" for event in events))
    report = compare_runs([tmp_path / "first" / "run", tmp_path / "second" / "run"])
    assert report["recorded_setup_matches"] is False
    assert any("guard evidence" in issue for issue in report["comparability_issues"])


def test_unguarded_runs_remain_supported_and_do_not_claim_a_check(tmp_path):
    result, events, manifest, policy, _ = run(tmp_path, None)
    assert result["ok"] and policy.calls == 1
    assert result["initial_state_check"] is None and manifest["initial_expectation"] is None
    assert not any(event["kind"] == "initial_state_check" for event in events)
