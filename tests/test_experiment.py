import copy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import time

import pytest

from dfeval.care import summarize_events
from dfeval.experiment import ExperimentConfig, run_experiment
from dfeval.model_observation import (MAX_MODEL_OBSERVATION_BYTES, MODEL_OBSERVATION_VERSION,
                                      model_input_bytes, project_observation)
from dfeval.policies import ChatCompletionsPolicy, IdlePolicy, RulePolicy, json_bytes


def snapshot(tick=100, drinks=5):
    return {"world_loaded": True, "map_loaded": True, "fortress_mode": True, "paused": True,
            "absolute_tick": tick, "df_version": "test-df", "dfhack_version": "test-dfhack",
            "save_directory": "test-save", "citizens": [{"id": 1, "stress": None, "needs": None}],
            "known_former_citizens": [], "stocks": {"by_item_type": {"DRINK": {"stack_units": drinks}}},
            "workshops": [{"id": 8, "type": "Still", "completed": True, "jobs": []}], "errors": []}


class Bridge:
    def __init__(self, game_dir, timeout):
        self.game_dir, self.timeout = game_dir, timeout
        self.session = "fake-bridge-for-tests"
        self.calls = []
        self.tick = 100
        self.loaded = True
        self.fail_advance = False
        self.fail_cleanup = False
        self.pauses = 0
        self.short_advance = False
    def install_script(self):
        path = self.game_dir / "test-bridge.lua"
        path.write_text("-- test fixture; never executed", encoding="utf-8")
        return path
    def status(self):
        self.calls.append(("status", {}))
        return {**snapshot(self.tick), "map_loaded": self.loaded}
    def observe(self):
        self.calls.append(("observe", {}))
        return snapshot(self.tick)
    def pause(self):
        self.calls.append(("pause", {}))
        self.pauses += 1
        if self.fail_cleanup and self.pauses > 1:
            raise RuntimeError("test pause failure")
        return {"paused": True}
    def queue_brew(self, **kwargs):
        self.calls.append(("queue_brew", kwargs))
        return {"queued_jobs": kwargs["quantity"], "job_ids": [3], "completed": False}
    def advance_ticks(self, ticks, timeout):
        self.calls.append(("advance_ticks", {"ticks": ticks}))
        if self.fail_advance:
            raise RuntimeError("test bridge failure")
        start_tick = self.tick
        elapsed = ticks - 1 if self.short_advance else ticks
        self.tick += elapsed
        return {"paused": True, "elapsed_ticks": elapsed, "requested_ticks": ticks,
                "start_absolute_tick": start_tick, "absolute_tick": self.tick}


@pytest.fixture
def harness(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    bridge = Bridge(game, 1)
    def run(policy=None, config=None, **kwargs):
        out = tmp_path / f"run-{len(list(tmp_path.glob('run-*')))}"
        result = run_experiment(game, policy or IdlePolicy(),
            config=config or ExperimentConfig(max_decisions=2, ticks_per_decision=10, max_total_ticks=20),
            output_dir=out, bridge_factory=lambda *args, **kw: bridge,
            bootstrap=lambda bridge, timeout: {"returncode": 0, "stdout": "test", "stderr": ""}, **kwargs)
        events = [json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        return result, events, manifest
    return run, bridge


def test_idle_run_real_contract_audit_replay_and_final_pause(harness):
    run, bridge = harness
    result, events, manifest = run()
    assert result["outcome"] == "budget_exhausted"
    assert result["ok"] is True
    assert result["detail"] == "max_decisions"
    assert result["is_model"] is False
    assert result["pause_confirmed"] is True
    assert bridge.calls[-1][0] == "pause"
    assert result["budgets"]["requested_ticks"] == 20
    assert result["budgets"]["decisions"] == 2
    assert result["summary"] == summarize_events(events)
    assert [event["event"] for event in events] == list(range(len(events)))
    assert all({"at", "wall_seconds", "turn", "kind"} <= event.keys() for event in events)
    assert [event["wall_seconds"] for event in events] == sorted(event["wall_seconds"] for event in events)
    assert events[0]["kind"] == "run_start" and events[-1]["kind"] == "run_end"
    first = next(e["snapshot"] for e in events if e["kind"] == "snapshot")
    assert manifest["initial_snapshot_sha256"] == hashlib.sha256(json_bytes(first)).hexdigest()
    assert manifest["initial_tick"] == 100
    assert manifest["df_version"] == "test-df"
    assert manifest["state"] == "finished"


def test_rule_brewing_uses_only_explicit_native_arguments(harness):
    run, bridge = harness
    result, events, _ = run(RulePolicy())
    assert ("queue_brew", {"workshop_id": 8, "quantity": 1}) in bridge.calls
    actions = [e for e in events if e["kind"] == "action_result" and e["operation"] == "queue_brew"]
    assert actions[0]["arguments"] == {"workshop_id": 8, "quantity": 1}
    assert actions[0]["result"]["completed"] is False


def test_runtime_product_summary_preserves_receipts_and_matches_saved_evidence(harness, monkeypatch):
    run, bridge = harness
    product = {"id": 1, "session": bridge.session, "epoch": 1,
               "source": "dfhack.eventful.onReactionComplete", "kind": "native_reaction_product",
               "reaction": "BREW_DRINK_FROM_PLANT", "job_id": 3, "workshop_id": 8,
               "worker_id": 1, "absolute_tick": 105, "item_next_id_before": 1000,
               "item_next_id_after": 1001, "dropped_outputs": 0, "errors": [],
               "outputs": [{"id": 1000, "item_type": "DRINK", "stack_size": 25, "newly_created": True}]}
    def observe():
        products = [copy.deepcopy(product)] if bridge.tick > 100 else []
        return {**snapshot(bridge.tick), "brewing": {
            "available": True, "session": bridge.session, "epoch": 1,
            "events": products, "event_count": len(products), "dropped_events": 0,
            "error_count": 0, "queued_jobs": len(products)}}
    def queue_brew(**arguments):
        return {"workshop_id": arguments["workshop_id"], "queued_jobs": arguments["quantity"],
                "job_ids": [3], "completed": False, "reaction": "BREW_DRINK_FROM_PLANT"}
    monkeypatch.setattr(bridge, "observe", observe)
    monkeypatch.setattr(bridge, "queue_brew", queue_brew)
    result, events, _ = run(RulePolicy(), ExperimentConfig(
        max_decisions=1, ticks_per_decision=10, max_total_ticks=10))
    assert result["ok"] is True
    assert result["summary"] == summarize_events(events)
    brewing = result["summary"]["brewing"]
    assert brewing["confirmed_new_drink_stack_units"] == 25
    assert brewing["receipt_linkage"] == "verified"
    assert brewing["evidence_complete"] is True


class FixedPolicy(IdlePolicy):
    def __init__(self, decision):
        self.decision = decision
    def choose(self, observation, history):
        return self.decision


def test_invalid_policy_output_recorded_and_never_dispatched(harness):
    run, bridge = harness
    result, events, _ = run(FixedPolicy({"action": "shell", "command": "unsafe"}))
    assert result["outcome"] == "error"
    assert result["ok"] is False
    assert result["pause_confirmed"] is True
    assert not any(call[0] in ("queue_brew", "advance_ticks") for call in bridge.calls)
    assert any(e["kind"] == "policy_response" and "unsafe" in e["exchange"]["response_text"] for e in events)
    assert events[-1]["kind"] == "run_end"


@pytest.mark.parametrize("field,value", [("max_total_ticks", 0), ("max_bridge_calls", 4)])
def test_budget_checked_before_brewing_mutation(harness, field, value):
    run, bridge = harness
    result, _, _ = run(RulePolicy(), replace(ExperimentConfig(), **{field: value}))
    assert result["outcome"] == "budget_exhausted"
    assert not any(call[0] in ("queue_brew", "advance_ticks") for call in bridge.calls)
    assert result["pause_confirmed"] is True


def test_policy_call_and_output_byte_budgets(harness):
    run, bridge = harness
    result, _, _ = run(config=ExperimentConfig(max_decisions=3, max_policy_calls=1))
    assert result["budgets"]["policy_calls"] == 1
    assert result["detail"] == "max_policy_calls"
    before = len(bridge.calls)
    result, events, _ = run(config=ExperimentConfig(max_output_bytes=1))
    assert result["detail"] == "max_output_bytes"
    assert not any(call[0] == "advance_ticks" for call in bridge.calls[before:])


def test_human_stop_before_bootstrap_and_during_policy(harness, tmp_path):
    run, bridge = harness
    stop = tmp_path / "human-stop"
    stop.write_text("stop")
    result, events, _ = run(config=ExperimentConfig(stop_file=str(stop)))
    assert result["outcome"] == "cancelled"
    assert result["ok"] is False
    assert not bridge.calls
    assert events[-1]["kind"] == "run_end"
    stop.unlink()
    class StopPolicy(IdlePolicy):
        def choose(self, observation, history):
            stop.write_text("stop")
            return super().choose(observation, history)
    result, _, _ = run(StopPolicy(), ExperimentConfig(stop_file=str(stop)))
    assert result["outcome"] == "cancelled"
    assert result["pause_confirmed"] is True
    assert not any(call[0] == "advance_ticks" for call in bridge.calls)


def test_slow_policy_result_is_discarded_at_deadline(harness):
    run, bridge = harness
    class Slow(IdlePolicy):
        def choose(self, observation, history):
            time.sleep(.3)
            return super().choose(observation, history)
    started = time.monotonic()
    result, _, _ = run(Slow(), ExperimentConfig(request_timeout=.05))
    assert time.monotonic() - started < .3
    assert result["outcome"] == "budget_exhausted"
    assert result["pause_confirmed"] is True
    assert not any(call[0] == "advance_ticks" for call in bridge.calls)


def test_not_loaded_is_error_without_mock_fallback(harness):
    run, bridge = harness
    bridge.loaded = False
    result, events, _ = run()
    assert result["outcome"] == "error"
    assert result["budgets"]["policy_calls"] == 0
    assert not any(event["kind"] == "snapshot" for event in events)
    assert result["summary"]["initial"] is None


def test_bridge_failure_and_cleanup_failure_remain_auditable(harness):
    run, bridge = harness
    bridge.fail_advance = True
    bridge.fail_cleanup = True
    result, events, _ = run()
    assert result["outcome"] == "error"
    assert result["pause_confirmed"] is False
    assert result["budgets"]["requested_ticks"] == 10
    assert result["budgets"]["advance_calls_with_unknown_elapsed"] == 1
    assert any(event["kind"] == "cleanup_error" for event in events)
    assert events[-1]["kind"] == "run_end"


def test_finish_uses_no_ticks_and_exact_input_is_saved(harness):
    run, bridge = harness
    policy = FixedPolicy({"action": "finish", "reason": "Done", "notebook": "Public"})
    result, events, _ = run(policy)
    assert result["outcome"] == "finished"
    assert result["budgets"]["requested_ticks"] == 0
    seen = next(e for e in events if e["kind"] == "policy_input")
    native = next(e for e in events if e["kind"] == "snapshot")
    assert seen["observation"] == project_observation(native["snapshot"])
    assert seen["history"] == []


def test_short_advance_is_recorded_then_ends_with_error(harness):
    run, bridge = harness
    bridge.short_advance = True
    result, events, _ = run()
    assert result["outcome"] == "error"
    assert result["summary"]["observed_tick_span"] == 9
    assert result["budgets"]["requested_ticks"] == 10


def test_model_token_budget_reserved_even_if_usage_missing(harness):
    run, bridge = harness
    requests = []
    class Opener:
        def open(self, req, timeout):
            requests.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"action": "wait", "reason": "", "notebook": ""})}}]}).encode())
    policy = ChatCompletionsPolicy(mode="local", model="explicit-test-model", opener=Opener())
    result, events, manifest = run(policy, ExperimentConfig(max_decisions=3, max_output_tokens=10))
    assert result["is_model"] is True
    assert requests[0]["max_tokens"] == 10
    assert len(requests) == 1
    assert result["budgets"]["output_tokens_reserved"] == 10
    assert result["budgets"]["calls_with_unknown_usage"] == 1
    assert result["detail"] == "max_output_tokens"
    assert manifest["policy"]["model"] == "explicit-test-model"
    assert manifest["model_observation_version"] == manifest["config"]["model_observation_version"] == MODEL_OBSERVATION_VERSION
    native = next(e["snapshot"] for e in events if e["kind"] == "snapshot")
    seen = next(e for e in events if e["kind"] == "policy_input")
    assert seen["observation"] == project_observation(native)
    encoded = model_input_bytes(seen["observation"], seen["history"])
    assert requests[0]["messages"][1]["content"].encode() == encoded
    assert seen["input_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert seen["input_bytes"] == len(encoded)
    assert manifest["initial_snapshot_sha256"] == hashlib.sha256(json_bytes(native)).hexdigest()
    assert any(e["kind"] == "policy_response" for e in events)


@pytest.mark.parametrize("kwargs", [
    {"max_decisions": 10001}, {"ticks_per_decision": 12001}, {"max_decisions": True},
    {"max_wall_seconds": float("inf")}, {"request_timeout": 0}, {"max_output_tokens": 0},
    {"starting_save_sha256": "not-a-hash"},
])
def test_invalid_config_rejected_before_game_or_output(kwargs):
    with pytest.raises(ValueError):
        ExperimentConfig(**kwargs)


def test_declared_save_identity_retained(harness):
    run, bridge = harness
    result, events, manifest = run(config=ExperimentConfig(max_decisions=1, starting_save_sha256="a" * 64))
    assert manifest["starting_save_sha256"] == "a" * 64
    assert manifest["config"]["starting_save_sha256"] == "a" * 64


def test_existing_nonempty_output_rejected_without_overwriting(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="new or empty"):
        run_experiment(game, IdlePolicy(), output_dir=output)
    assert (output / "keep.txt").read_text() == "keep"


def test_log_budget_ends_with_complete_record_and_pause(harness):
    run, bridge = harness
    original_observe = bridge.observe
    def large_observation():
        return {**original_observe(), "test_native_text": "x" * 900_000}
    bridge.observe = large_observation
    result, events, manifest = run(config=ExperimentConfig(max_log_bytes=1_000_000))
    assert result["detail"] == "max_log_bytes"
    assert events[-1]["kind"] == "run_end"
    assert result["pause_confirmed"] is True
    assert manifest["event_bytes"] <= 1_000_000
    assert not any(call[0] == "advance_ticks" for call in bridge.calls)


def test_huge_snapshot_is_not_presented_to_model(harness):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "test_native_text": "x" * 2000}
    result, events, _ = run(config=ExperimentConfig(max_snapshot_bytes=1024))
    assert result["outcome"] == "budget_exhausted"
    assert result["budgets"]["policy_calls"] == 0
    assert not any(event["kind"] == "policy_input" for event in events)
    assert result["pause_confirmed"] is True


def test_projection_cap_retains_raw_snapshot_then_stops_without_a_policy_call(harness):
    run, bridge = harness
    native = snapshot()
    native["citizens"][0]["name"] = "x" * MAX_MODEL_OBSERVATION_BYTES
    bridge.observe = lambda: native
    result, events, manifest = run()
    assert result["outcome"] == "budget_exhausted"
    assert "max_model_observation_bytes" in result["detail"]
    assert result["budgets"]["policy_calls"] == 0 and result["pause_confirmed"] is True
    assert next(e["snapshot"] for e in events if e["kind"] == "snapshot") == native
    assert manifest["initial_snapshot_sha256"] == hashlib.sha256(json_bytes(native)).hexdigest()
    assert not any(e["kind"] == "policy_input" for e in events)
    assert not any(operation in ("advance_ticks", "queue_brew") for operation, _ in bridge.calls)


def test_input_limit_includes_public_history_and_projected_body(harness):
    run, bridge = harness
    native = snapshot()
    native["citizens"][0]["name"] = "x" * 1100
    bridge.observe = lambda: native
    result, events, _ = run(config=ExperimentConfig(max_input_bytes=1024))
    assert result["detail"] == "max_input_bytes; input was not sent"
    assert result["budgets"]["policy_calls"] == 0 and result["pause_confirmed"] is True
    assert not any(e["kind"] == "policy_input" for e in events)


@pytest.mark.parametrize("failure,outcome", [("too_large", "budget_exhausted"), ("wrong_version", "error")])
def test_projection_failure_restores_fps_and_pause_without_model_or_gameplay_calls(harness, failure, outcome):
    run, bridge = harness
    native = snapshot()
    if failure == "too_large":
        native["citizens"][0]["name"] = "x" * MAX_MODEL_OBSERVATION_BYTES
    else:
        native["model_observation_version"] = "unsupported"
    bridge.observe = lambda: native
    def set_fps(fps):
        bridge.calls.append(("set_simulation_fps", {"fps": fps}))
        return {"simulation_fps": {"effective": fps}}
    def restore_fps():
        bridge.calls.append(("restore_simulation_fps", {}))
        return {"restored": True}
    bridge.set_simulation_fps, bridge.restore_simulation_fps = set_fps, restore_fps
    class NeverContact:
        def open(self, *args, **kwargs):
            pytest.fail("Projection rejection must precede provider access")
    policy = ChatCompletionsPolicy(mode="local", model="test", opener=NeverContact())
    result, events, _ = run(policy, config=ExperimentConfig(simulation_fps=1000))
    assert result["outcome"] == outcome
    assert result["speed_restored"] is True and result["pause_confirmed"] is True
    assert result["budgets"]["policy_calls"] == result["budgets"]["requested_ticks"] == 0
    assert [operation for operation, _ in bridge.calls][-2:] == ["pause", "restore_simulation_fps"]
    assert not any(operation in ("advance_ticks", "queue_brew") for operation, _ in bridge.calls)
    assert next(e["snapshot"] for e in events if e["kind"] == "snapshot") == native
    assert events[-1]["kind"] == "run_end" and events[-1]["speed_restored"] is True


def test_policy_cannot_modify_saved_native_snapshots_or_select_operation_names(harness):
    run, bridge = harness
    class MutatingPolicy(IdlePolicy):
        def choose(self, observation, history):
            observation["citizens"][0]["stress"] = 1234
            return {"action": "finish", "reason": "Public", "notebook": ""}
    result, events, _ = run(MutatingPolicy())
    native = next(e["snapshot"] for e in events if e["kind"] == "snapshot")
    seen = next(e["observation"] for e in events if e["kind"] == "policy_input")
    assert native["citizens"][0]["stress"] is None
    assert seen == project_observation(native)
    assert result["summary"]["final"]["citizen_measurements"]["stress"]["mean"] is None


def test_history_window_is_explicit_and_exact(harness):
    run, bridge = harness
    result, events, manifest = run(config=ExperimentConfig(
        max_decisions=3, history_decisions=1, ticks_per_decision=1))
    seen = [e for e in events if e["kind"] == "policy_input"]
    decisions = [e["decision"] for e in events if e["kind"] == "decision"]
    assert seen[0]["history"] == []
    assert seen[1]["history"] == [decisions[0]]
    assert seen[2]["history"] == [decisions[1]]
    assert manifest["config"]["history_decisions"] == 1


def test_initially_unpaused_observation_stops_before_policy(harness):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "paused": False}
    result, _, _ = run()
    assert result["outcome"] == "error"
    assert result["budgets"]["policy_calls"] == 0
    assert result["pause_confirmed"] is True


@pytest.mark.parametrize("citizens", [[], [{"id": 1, "dead": True}]])
def test_empty_or_confirmed_dead_roster_ends_with_snapshot_and_pause(harness, citizens):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "citizens": citizens}
    result, events, _ = run()
    assert result["outcome"] == "no_citizens"
    assert result["ok"] is False
    assert result["pause_confirmed"] is True
    assert result["budgets"]["policy_calls"] == 0
    assert result["summary"]["final"]["population"] == 0
    assert any(event["kind"] == "snapshot" for event in events)
    assert not any(call[0] == "advance_ticks" for call in bridge.calls)


@pytest.mark.parametrize("citizens", [None, [{"id": 1, "dead": None}], [{"id": 1}]])
def test_unknown_roster_or_death_flag_is_not_extinction(harness, citizens):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "citizens": citizens}
    result, _, _ = run(FixedPolicy({"action": "finish", "reason": "", "notebook": ""}))
    assert result["outcome"] == "finished"


def test_invalid_roster_is_error_not_extinction(harness):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "citizens": [None]}
    result, _, _ = run()
    assert result["outcome"] == "error"
    assert result["summary"]["final"]["population"] is None
    assert result["pause_confirmed"] is True


def test_extinction_after_advance_retains_final_native_snapshot(harness):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "citizens": [] if bridge.tick > 100 else [{"id": 1}]}
    result, events, _ = run()
    assert result["outcome"] == "no_citizens"
    assert result["budgets"]["decisions"] == 1
    assert result["summary"]["snapshot_count"] == 2
    assert result["summary"]["population_change"] == -1
    # No death is inferred just from population loss.
    assert result["summary"]["observed_confirmed_deaths"] == 0


def test_external_clock_movement_between_decisions_invalidates_run(harness):
    run, bridge = harness
    class ExternalMovement(IdlePolicy):
        def choose(self, observation, history):
            bridge.tick += 100
            return super().choose(observation, history)
    result, events, _ = run(ExternalMovement())
    assert result["outcome"] == "error"
    assert "clock continuity" in result["detail"]
    assert result["summary"]["observed_tick_span"] == 110
    assert result["budgets"]["requested_ticks"] == 10
    assert result["pause_confirmed"] is True
    assert sum(e["kind"] == "snapshot" for e in events) == 2


@pytest.mark.parametrize("field,value", [("start_absolute_tick", 99), ("absolute_tick", 111)])
def test_false_advance_boundaries_rejected_even_when_elapsed_and_snapshot_match(harness, field, value):
    run, bridge = harness
    original_advance = bridge.advance_ticks
    bridge.advance_ticks = lambda **kwargs: {**original_advance(**kwargs), field: value}
    result, events, _ = run()
    assert result["outcome"] == "error"
    assert "tick boundaries" in result["detail"]
    assert result["summary"]["snapshot_count"] == 2
    assert result["pause_confirmed"] is True


def test_save_change_preserves_snapshot_but_invalidates_run(harness):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), "save_directory": "other-save" if bridge.tick > 100 else "test-save"}
    result, events, _ = run()
    assert result["outcome"] == "error"
    assert "save identity changed" in result["detail"]
    assert [e["snapshot"]["save_directory"] for e in events if e["kind"] == "snapshot"] == ["test-save", "other-save"]
    assert result["pause_confirmed"] is True


@pytest.mark.parametrize("field,value", [("absolute_tick", None), ("absolute_tick", True), ("save_directory", None)])
def test_unknown_clock_or_save_identity_cannot_validate_bounded_run(harness, field, value):
    run, bridge = harness
    original_observe = bridge.observe
    bridge.observe = lambda: {**original_observe(), field: value}
    result, events, _ = run()
    assert result["outcome"] == "error"
    assert result["budgets"]["policy_calls"] == 0
    assert result["summary"]["snapshot_count"] == 1
    assert result["pause_confirmed"] is True
