from dataclasses import asdict
import copy
import hashlib
import json

import pytest

from dfeval.comparison import (COMPARISON_FIELDS, _evidence_json, _model_input_contract, _reported_fps,
                               comparable_native_state, compare_runs, initial_observation_fingerprint,
                               read_run, render_comparison_report)
from dfeval.experiment import ExperimentConfig, run_experiment
from dfeval.live import PROTOCOL_VERSION
from dfeval.model_observation import (MODEL_OBSERVATION_VERSION, model_input_bytes,
                                      project_observation, projection_contract)
from dfeval.local_models import OllamaPolicy
from dfeval.policies import (ChatCompletionsPolicy, DecisionError, IdlePolicy, LEGACY_SYSTEM_PROMPT,
                             SYSTEM_PROMPT, json_bytes)


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


def model_contract_record(root, *, provider="ollama", prompt=SYSTEM_PROMPT, history_limit=2,
                          call_caps=(512, 512, 256)):
    """Recorded three-turn fixture; constructing a policy never contacts a server."""
    record(root)
    if provider == "ollama":
        policy = OllamaPolicy(model="recorded-model", system_prompt=prompt).public_config()
    elif provider == "cloud":
        policy = ChatCompletionsPolicy(mode="cloud", model="recorded-model", system_prompt=prompt,
                                       endpoint="https://models.example/v1/chat/completions",
                                       api_key_env="DFEVAL_TEST_KEY").public_config()
    else:
        policy = ChatCompletionsPolicy(mode="local", model="recorded-model", system_prompt=prompt).public_config()
    manifest = json.loads((root / "manifest.json").read_text())
    config = manifest["config"]
    config.update(policy=policy, model_observation_version=MODEL_OBSERVATION_VERSION,
                  history_decisions=history_limit, max_output_tokens=sum(call_caps) or 1,
                  max_decisions=max(1, len(call_caps)), max_total_ticks=1200 * len(call_caps))
    manifest.update(policy=policy, model_observation_version=MODEL_OBSERVATION_VERSION,
                    model_observation=projection_contract())
    events = [{"kind": "run_start", "turn": 0, "config": config},
              {"kind": "snapshot", "turn": 0, "snapshot": snapshot()}]
    decisions = []
    for turn, cap in enumerate(call_caps, 1):
        seen = project_observation(events[-1]["snapshot"])
        history = copy.deepcopy(decisions[-history_limit:]) if history_limit else []
        body = model_input_bytes(seen, history)
        request = {"model": policy["model"], "messages": [
            {"role": "system", "content": prompt}, {"role": "user", "content": body.decode()}], "stream": False}
        if provider == "ollama":
            request.update(format=policy["response_format"]["json_schema"]["schema"],
                           options={**policy["options"], "num_predict": cap},
                           keep_alive=policy["keep_alive"], think=policy["think"])
        else:
            request.update(response_format=policy["response_format"], **{policy["token_limit_field"]: cap})
        decision = {"action": "wait", "reason": f"Public reason {turn}", "notebook": f"Public note {turn}"}
        message = {"role": "assistant", "content": json.dumps(decision)}
        response = ({"message": message, "done": True, "done_reason": "stop"} if provider == "ollama" else
                    {"choices": [{"message": message, "finish_reason": "stop"}]})
        response_text = json.dumps(response)
        events.extend([
            {"kind": "policy_input", "turn": turn, "observation": seen, "history": history,
             "model_observation_version": MODEL_OBSERVATION_VERSION, "input_bytes": len(body),
             "input_sha256": hashlib.sha256(body).hexdigest()},
            {"kind": "policy_response", "turn": turn, "error": None, "duration_seconds": 0.1,
             "exchange": {"request": request, "response_text": response_text,
                          "response_bytes": len(response_text.encode()), "response_truncated": False,
                          "reserved_output_tokens": cap}},
            {"kind": "decision", "turn": turn, "decision": copy.deepcopy(decision)},
            {"kind": "action_result", "turn": turn, "operation": "advance_ticks", "arguments": {"ticks": 1200},
             "result": {"paused": True, "elapsed_ticks": 1200, "requested_ticks": 1200,
                        "start_absolute_tick": 100 + (turn - 1) * 1200, "absolute_tick": 100 + turn * 1200}},
            {"kind": "snapshot", "turn": turn, "snapshot": snapshot(100 + turn * 1200)},
        ])
        decisions.append(decision)
    events.append({"kind": "run_end", "turn": len(call_caps), "outcome": "budget_exhausted",
                   "detail": "max_decisions" if call_caps else "max_wall_seconds", "pause_confirmed": True})
    for index, event in enumerate(events):
        event.update(event=index, at="2026-09-05T12:00:00+00:00", wall_seconds=float(index))
    (root / "manifest.json").write_text(json.dumps(manifest))
    write_events(root, events)
    return root


def contract_at(root):
    manifest = json.loads((root / "manifest.json").read_text())
    warnings = []
    version, verified = _model_input_contract(manifest, manifest["config"], manifest["policy"], events_at(root), warnings.append)
    assert version == MODEL_OBSERVATION_VERSION
    return verified, warnings


@pytest.mark.parametrize("provider", ["ollama", "chat_completions", "cloud"])
@pytest.mark.parametrize("prompt", [SYSTEM_PROMPT, LEGACY_SYSTEM_PROMPT])
@pytest.mark.parametrize("history_limit", [0, 1, 2])
def test_model_contract_accepts_recorded_briefings_and_tightened_call_limits(tmp_path, provider, prompt, history_limit):
    root = model_contract_record(tmp_path / "source", provider=provider, prompt=prompt, history_limit=history_limit)
    assert contract_at(root) == (True, [])
    assert read_run(root)["model_input_contract_verified"] is True


@pytest.mark.parametrize("change", ["fabricated", "reordered", "future", "too_many", "missing"])
def test_rehashed_history_must_come_from_prior_accepted_decisions(tmp_path, change):
    root = model_contract_record(tmp_path / "source", history_limit=1 if change == "too_many" else 2)
    events = events_at(root)
    seen = next(event for event in events if event["kind"] == "policy_input" and event["turn"] == 3)
    decisions = [event["decision"] for event in events if event["kind"] == "decision"]
    if change == "fabricated":
        seen["history"][0]["notebook"] = "This notebook was never accepted."
    elif change == "reordered":
        seen["history"].reverse()
    elif change == "future":
        seen["history"][-1] = decisions[2]
    elif change == "too_many":
        seen["history"] = decisions[:2]
    else:
        seen["history"] = []
    body = model_input_bytes(seen["observation"], seen["history"])
    seen.update(input_bytes=len(body), input_sha256=hashlib.sha256(body).hexdigest())
    response = next(event for event in events if event["kind"] == "policy_response" and event["turn"] == 3)
    response["exchange"]["request"]["messages"][1]["content"] = body.decode()
    write_events(root, events)
    verified, warnings = contract_at(root)
    assert verified is False
    assert any("preceding accepted decisions" in warning for warning in warnings)


@pytest.mark.parametrize("change", ["system", "model", "message_order", "extra_message", "extra_field",
                                   "temperature", "seed", "context", "boolean_number", "format", "stream", "cap"])
def test_declared_model_request_settings_cannot_be_replaced(tmp_path, change):
    root = model_contract_record(tmp_path / "source")
    events = events_at(root)
    request = next(event for event in events if event["kind"] == "policy_response")["exchange"]["request"]
    if change == "system":
        request["messages"][0]["content"] = "A different objective."
    elif change == "model":
        request["model"] = "another-model"
    elif change == "message_order":
        request["messages"].reverse()
    elif change == "extra_message":
        request["messages"].append({"role": "user", "content": "Extra instructions"})
    elif change == "extra_field":
        request["tools"] = []
    elif change in ("temperature", "seed"):
        request["options"][change] = 1
    elif change == "boolean_number":
        request["options"]["temperature"] = False
    elif change == "context":
        request["options"]["num_ctx"] *= 2
    elif change == "format":
        request["format"] = {"type": "object"}
    elif change == "stream":
        request["stream"] = True
    else:
        request["options"]["num_predict"] = 513
    write_events(root, events)
    verified, warnings = contract_at(root)
    assert verified is False
    assert any("declared provider settings" in warning for warning in warnings)


def test_compatible_request_cannot_change_token_limit_field_or_exceed_remaining_budget(tmp_path):
    for change in ("field", "remaining"):
        root = model_contract_record(tmp_path / change, provider="chat_completions")
        events = events_at(root)
        response = [event for event in events if event["kind"] == "policy_response"][-1]
        request = response["exchange"]["request"]
        if change == "field":
            request["max_completion_tokens"] = request.pop("max_tokens")
        else:
            request["max_tokens"] = 257  # Below the per-call cap, but above the 256 remaining tokens.
        write_events(root, events)
        assert contract_at(root)[0] is False


def test_tightened_transport_limit_does_not_refund_the_runner_token_reservation(tmp_path):
    root = model_contract_record(tmp_path / "source")
    events = events_at(root)
    responses = [event for event in events if event["kind"] == "policy_response"]
    responses[0]["exchange"]["request"]["options"]["num_predict"] = 128
    write_events(root, events)
    assert contract_at(root) == (True, [])
    responses[-1]["exchange"]["request"]["options"]["num_predict"] = 300
    write_events(root, events)
    # The runner still reserved 512 on turn one; only 256 remain on turn three.
    assert contract_at(root)[0] is False


def test_missing_request_for_an_accepted_model_decision_is_unknown(tmp_path):
    root = model_contract_record(tmp_path / "source")
    events = events_at(root)
    next(event for event in events if event["kind"] == "policy_response")["exchange"].pop("request")
    write_events(root, events)
    verified, warnings = contract_at(root)
    assert verified is None
    assert any("lacks recorded request evidence" in warning for warning in warnings)


@pytest.mark.parametrize("has_response", [False, True])
def test_failed_call_without_request_evidence_remains_unknown_without_new_repeat_blocker(tmp_path, has_response):
    root = model_contract_record(tmp_path / "source")
    events = events_at(root)
    input_index = next(index for index, event in enumerate(events) if event["kind"] == "policy_input")
    kept = events[:input_index + 1]
    if has_response:
        kept.append({"kind": "policy_response", "turn": 1, "error": {"type": "PolicyError", "message": "Unavailable"},
                     "exchange": {"request": None, "response_text": None, "response_bytes": 0}})
    kept.extend([{"kind": "error", "turn": 1, "type": "PolicyError", "message": "Unavailable"},
                 {"kind": "run_end", "turn": 1, "outcome": "error", "pause_confirmed": True}])
    for index, event in enumerate(kept):
        event.update(event=index, at="2026-09-05T12:00:00+00:00", wall_seconds=float(index))
    write_events(root, kept)
    assert contract_at(root) == (None, [])
    warnings = read_run(root)["warnings"]
    assert all(warning == "The event stream records an action, run, or cleanup failure."
               or warning.startswith("Terminal outcome is ") for warning in warnings)


def test_zero_call_model_recording_has_no_verified_input_claim(tmp_path):
    root = model_contract_record(tmp_path / "source", call_caps=())
    assert contract_at(root) == (None, [])


def test_unknown_system_prompt_is_not_filled_from_todays_default(tmp_path):
    root = model_contract_record(tmp_path / "source")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["policy"].pop("system_prompt")
    manifest["config"]["policy"].pop("system_prompt")
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert contract_at(root)[0] is None


def test_history_cannot_be_established_by_a_decision_that_differs_from_its_response(tmp_path):
    root = model_contract_record(tmp_path / "source")
    events = events_at(root)
    next(event for event in events if event["kind"] == "decision")["decision"]["notebook"] = "Never generated."
    write_events(root, events)
    verified, warnings = contract_at(root)
    assert verified is False
    assert any("cannot establish accepted public history" in warning for warning in warnings)


def brew_contract_record(root):
    model_contract_record(root, call_caps=(512,))
    events = events_at(root)
    decision = {"action": "brew", "workshop_id": 9, "quantity": 1,
                "reason": "A recorded request for brewing.", "notebook": "Inspect the consequences."}
    next(event for event in events if event["kind"] == "decision")["decision"] = decision
    exchange = next(event for event in events if event["kind"] == "policy_response")["exchange"]
    envelope = json.loads(exchange["response_text"])
    envelope["message"]["content"] = json.dumps(decision)
    exchange.update(response_text=json.dumps(envelope), response_bytes=len(json.dumps(envelope).encode()))
    index = next(index for index, event in enumerate(events) if event.get("operation") == "advance_ticks")
    events.insert(index, {"kind": "action_result", "turn": 1, "operation": "queue_brew",
                          "arguments": {"workshop_id": 9, "quantity": 1},
                          "result": {"workshop_id": 9, "queued_jobs": 1, "job_ids": [71],
                                     "completed": False, "reaction": "BREW_DRINK_FROM_PLANT"}})
    for index, event in enumerate(events):
        event.update(event=index, at="2026-09-05T12:00:00+00:00", wall_seconds=float(index))
    write_events(root, events)
    return root


def test_contradictory_dispatch_qualifies_comparison_even_when_input_and_response_agree(tmp_path):
    left, right = [brew_contract_record(tmp_path / name) for name in ("reference", "contradiction")]
    assert read_run(left)["action_evidence_verified"] is True
    events = events_at(right)
    next(event for event in events if event.get("operation") == "queue_brew")["arguments"]["workshop_id"] = 10
    write_events(right, events)
    report = compare_runs([left, right])
    assert report["runs"][1]["model_input_contract_verified"] is True
    assert report["runs"][1]["action_evidence_verified"] is False
    assert report["recorded_setup_matches"] is False
    assert any("response/action evidence contains contradictions" in issue for issue in report["comparability_issues"])


def test_failed_native_dispatch_remains_unknown_without_a_new_repeat_blocking_warning(tmp_path):
    root = brew_contract_record(tmp_path / "source")
    events = events_at(root)
    queue_index = next(index for index, event in enumerate(events) if event.get("operation") == "queue_brew")
    kept = events[:queue_index + 1]
    kept[-1].pop("result")
    kept[-1]["error"] = {"type": "LiveBridgeError", "message": "Native queue call failed."}
    kept.extend([{"kind": "error", "turn": 1, "type": "LiveBridgeError", "message": "Native queue call failed."},
                 {"kind": "run_end", "turn": 1, "outcome": "error", "pause_confirmed": True}])
    for index, event in enumerate(kept):
        event.update(event=index, at="2026-09-05T12:00:00+00:00", wall_seconds=float(index))
    write_events(root, kept)
    checked = read_run(root)
    assert checked["model_input_contract_verified"] is True
    assert checked["action_evidence_verified"] is None
    assert all(warning == "The event stream records an action, run, or cleanup failure."
               or warning.startswith("Terminal outcome is ") for warning in checked["warnings"])


def test_comparable_native_state_keeps_historical_hash_and_is_an_independent_tree():
    raw = snapshot()
    raw.update(ui_focus="dwarfmode/Default", known_former_citizens=[{"id": 9}],
               brewing={"session": "private", "epoch": 7, "events": [{"id": 12}], "queued_jobs": [5],
                        "event_count": 1, "dropped_events": 0, "error_count": 0, "last_error": None,
                        "notes": "session bookkeeping", "reaction": "native-reaction", "future_measurement": [2, 1]},
               simulation_fps={"effective": 10000.0, "graphics_cap": 50, "original": 100,
                               "requested": 10000, "original_graphics_cap": 50, "override_active": True,
                               "restore_error": None, "capture_error": None, "restore_reason": "test"})
    expected = snapshot()
    expected.pop("paused")
    expected.update(brewing={"reaction": "native-reaction", "future_measurement": [2, 1]},
                    simulation_fps={"effective": 10000.0, "graphics_cap": 50})
    historical_bytes = json.dumps(expected, sort_keys=True, ensure_ascii=True, allow_nan=False,
                                  separators=(",", ":")).encode()
    projected = comparable_native_state(raw)
    assert projected == expected
    assert initial_observation_fingerprint(raw) == hashlib.sha256(historical_bytes).hexdigest()
    projected["citizens"][0]["stress"] = 999
    projected["brewing"]["future_measurement"].reverse()
    assert raw["citizens"][0]["stress"] == 0
    assert raw["brewing"]["future_measurement"] == [2, 1]


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
