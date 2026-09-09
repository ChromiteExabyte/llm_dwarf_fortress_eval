import copy
import json

import pytest

from dfeval import action_evidence
from dfeval.action_evidence import audit_action_evidence


def numbered(events):
    for index, event in enumerate(events):
        event["event"] = index
    return events


def recording(provider="ollama", action="brew"):
    policy = {"kind": provider, "is_model": provider != "rule", "max_completion_tokens": 512}
    config = {"ticks_per_decision": 17}
    decision = {"action": action, "reason": "My public reason.", "notebook": "Care is mine to interpret."}
    if action == "brew":
        decision.update(workshop_id=3, quantity=2)
    message = {"role": "assistant", "content": json.dumps(decision)}
    if provider == "ollama":
        body = {"done": True, "done_reason": "stop", "message": message, "eval_count": 42}
    elif provider == "chat_completions":
        body = {"choices": [{"finish_reason": "stop", "message": message}],
                "usage": {"completion_tokens": 42}}
    else:
        body = decision
    events = [
        {"kind": "run_start", "turn": 0, "config": config},
        {"kind": "snapshot", "turn": 0,
         "snapshot": {"absolute_tick": 100, "paused": True, "save_directory": "fixture"}},
        {"kind": "policy_input", "turn": 1, "history": []},
        {"kind": "policy_response", "turn": 1, "error": None,
         "exchange": {"response_text": json.dumps(body), "response_truncated": False,
                      "reserved_output_tokens": 512}},
        {"kind": "decision", "turn": 1, "decision": decision},
    ]
    if action == "brew":
        events.append({"kind": "action_result", "turn": 1, "operation": "queue_brew",
                       "arguments": {"workshop_id": 3, "quantity": 2},
                       "result": {"workshop_id": 3, "queued_jobs": 2, "job_ids": [71, 72],
                                  "reaction": "BREW_DRINK_FROM_PLANT", "completed": False}})
    if action != "finish":
        events.extend([
            {"kind": "action_result", "turn": 1, "operation": "advance_ticks", "arguments": {"ticks": 17},
             "result": {"requested_ticks": 17, "elapsed_ticks": 17, "start_absolute_tick": 100,
                        "absolute_tick": 117, "overshoot_ticks": 0, "paused": True, "save_directory": "fixture"}},
            {"kind": "snapshot", "turn": 1,
             "snapshot": {"absolute_tick": 117, "paused": True, "save_directory": "fixture"}},
        ])
    events.append({"kind": "run_end", "turn": 1, "outcome": "finished" if action == "finish" else "budget_exhausted",
                   "detail": "Policy requested finish" if action == "finish" else "max_decisions"})
    return numbered(events), policy, config


def event_at(events, name):
    return next(event for event in events if event.get("operation", event["kind"]) == name)


def audit(data):
    return audit_action_evidence(*data)


def codes(result):
    return {issue["code"] for issue in result["issues"]}


@pytest.mark.parametrize("provider", ["ollama", "chat_completions", "rule"])
@pytest.mark.parametrize("action", ["wait", "brew", "finish"])
def test_complete_response_decision_and_native_chain(provider, action):
    data = recording(provider, action)
    original = copy.deepcopy(data)
    result = audit(data)
    assert result["verified"] is True
    assert result["issues"] == []
    turn = result["turns"][0]
    assert turn["decision"] == {"value": event_at(data[0], "decision")["decision"],
                                "schema_verified": True, "agreement_verified": True}
    assert turn["response"]["state"] == "accepted"
    if action == "brew":
        assert turn["queue"]["accepted"] is True
        assert turn["queue"]["job_ids"] == [71, 72]
        assert "completed" not in turn["queue"]
        assert "cancelled" not in turn["queue"]
    if action != "finish":
        assert turn["snapshot_alignment"]["before_tick"] == 100
        assert turn["snapshot_alignment"]["after_tick"] == 117
        assert turn["advance"]["state"] == "confirmed"
    assert data == original
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("field", ["action", "reason", "notebook", "quantity", "workshop_id"])
def test_exact_response_agreement_includes_public_words(field):
    data = recording()
    decision = event_at(data[0], "decision")["decision"]
    decision[field] = {"action": "wait", "reason": "Replaced reason", "notebook": "Replaced note",
                       "quantity": 1, "workshop_id": 4}[field]
    if field == "action":
        decision.pop("quantity")
        decision.pop("workshop_id")
    result = audit(data)
    assert result["verified"] is False
    assert result["turns"][0]["decision"]["agreement_verified"] is False
    assert "response_decision_mismatch" in codes(result)


@pytest.mark.parametrize("change", [{"action": "dig"}, {"workshop_id": True}, {"quantity": 0}, {"extra": "field"}])
def test_accepted_decision_must_pass_the_actual_schema(change):
    data = recording()
    event_at(data[0], "decision")["decision"].update(change)
    result = audit(data)
    assert result["verified"] is False
    assert result["turns"][0]["decision"]["schema_verified"] is False
    assert result["turns"][0]["decision"]["value"] is None


@pytest.mark.parametrize("failure", ["error", "truncated", "length", "bad_json", "duplicate_key", "tools", "too_many_tokens"])
@pytest.mark.parametrize("provider", ["ollama", "chat_completions"])
def test_rejected_provider_output_without_an_action_is_an_outcome_not_a_contradiction(failure, provider):
    data = recording(provider, "wait")
    response = event_at(data[0], "policy_response")
    exchange = response["exchange"]
    body = json.loads(exchange["response_text"])
    if failure == "error":
        response["error"] = {"type": "PolicyError", "message": "Call failed"}
    elif failure == "truncated":
        exchange["response_truncated"] = True
    elif failure == "bad_json":
        exchange["response_text"] = "unfinished {"
    elif failure == "duplicate_key":
        exchange["response_text"] = '{"message":{},"message":{}}'
    else:
        if failure == "length":
            if provider == "ollama":
                body["done_reason"] = "length"
            else:
                body["choices"][0]["finish_reason"] = "length"
        elif failure == "tools":
            message = body["message"] if provider == "ollama" else body["choices"][0]["message"]
            message["tool_calls"] = [{"function": "unavailable"}]
        else:
            if provider == "ollama":
                body["eval_count"] = 513
            else:
                body["usage"]["completion_tokens"] = 513
        exchange["response_text"] = json.dumps(body)
    data[0][:] = [event for event in data[0] if event["kind"] not in ("decision", "action_result")
                  and not (event["kind"] == "snapshot" and event["turn"] == 1)]
    numbered(data[0])
    result = audit(data)
    assert result["invalid_issue_count"] == 0
    assert result["verified"] is None
    assert result["turns"][0]["response"]["state"] == ("truncated" if failure in ("length", "truncated") else "failed")


@pytest.mark.parametrize("failure", ["error", "truncated"])
def test_accepted_decision_after_failed_response_is_contradictory(failure):
    data = recording()
    response = event_at(data[0], "policy_response")
    if failure == "error":
        response["error"] = {"type": "PolicyError"}
    else:
        response["exchange"]["response_truncated"] = True
    result = audit(data)
    assert result["turns"][0]["decision"]["agreement_verified"] is False
    assert "failed_response_accepted" in codes(result)


@pytest.mark.parametrize("missing", ["event", "exchange", "response_text"])
def test_missing_historical_response_is_unknown(missing):
    data = recording()
    response = event_at(data[0], "policy_response")
    if missing == "event":
        data[0].remove(response)
    elif missing == "exchange":
        response.pop("exchange")
    else:
        response["exchange"].pop("response_text")
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["decision"]["schema_verified"] is True
    assert result["turns"][0]["decision"]["agreement_verified"] is None


@pytest.mark.parametrize("name", ["policy_input", "policy_response", "decision", "queue_brew", "advance_ticks"])
def test_duplicate_turn_records_are_not_silently_chosen(name):
    data = recording()
    event = event_at(data[0], name)
    data[0].insert(data[0].index(event), copy.deepcopy(event))
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is False
    assert f"duplicate_{name}" in codes(result)
    if name in ("policy_response", "decision"):
        assert result["turns"][0]["decision"]["agreement_verified"] is False


@pytest.mark.parametrize("first,second,code", [("policy_response", "decision", "response_order"),
                                                ("decision", "queue_brew", "dispatch_order"),
                                                ("queue_brew", "advance_ticks", "queue_advance_order")])
def test_native_chain_uses_original_encounter_order(first, second, code):
    data = recording()
    a, b = (data[0].index(event_at(data[0], name)) for name in (first, second))
    data[0][a], data[0][b] = data[0][b], data[0][a]
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is False
    assert code in codes(result)


@pytest.mark.parametrize("arguments", [{"workshop_id": 9, "quantity": 2}, {"workshop_id": 3, "quantity": True},
                                        {"workshop_id": 3, "quantity": 2, "extra": 1}])
def test_queue_arguments_mismatch_is_invalid_even_when_native_call_failed(arguments):
    data = recording()
    event_at(data[0], "queue_brew").update(arguments=arguments, result=None, error={"type": "TimeoutError"})
    result = audit(data)
    assert result["verified"] is False
    assert "queue_arguments_mismatch" in codes(result)
    assert result["turns"][0]["queue"]["accepted"] is None


@pytest.mark.parametrize("receipt", [{"job_ids": [71, 71]}, {"job_ids": [True, 72]}, {"job_ids": [71]},
                                     {"job_ids": [{}, 72]}, {"queued_jobs": 1}, {"workshop_id": 9},
                                     {"reaction": "OTHER_REACTION"}, {"completed": True}])
def test_queue_receipt_must_confirm_exact_native_job_identity_and_count(receipt):
    data = recording()
    event_at(data[0], "queue_brew")["result"].update(receipt)
    result = audit(data)
    assert result["verified"] is False
    assert result["turns"][0]["queue"]["accepted"] is None


def test_native_queue_error_leaves_acceptance_unknown():
    data = recording()
    event_at(data[0], "queue_brew").update(result=None, error={"type": "CancelledError"})
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["queue"]["state"] == "attempted"
    assert result["turns"][0]["queue"]["accepted"] is None


@pytest.mark.parametrize("detail,expected", [
    ("max_total_ticks", "not_dispatched"),
    ("max_bridge_calls; action and following observation require more calls", "not_dispatched"),
    ("max_log_bytes", "unknown"), ("cancelled", "unknown"), ("max_wall_seconds", "unknown"),
])
@pytest.mark.parametrize("action", ["wait", "brew"])
def test_only_explicit_pre_dispatch_stops_confirm_an_unexecuted_accepted_decision(detail, expected, action):
    data = recording(action=action)
    data[0][:] = [event for event in data[0] if event["kind"] != "action_result"
                  and not (event["kind"] == "snapshot" and event["turn"] == 1)]
    event_at(data[0], "run_end").update(outcome="budget_exhausted", detail=detail)
    numbered(data[0])
    result = audit(data)
    assert result["turns"][0]["dispatch"]["state"] == expected
    assert result["verified"] is (True if expected == "not_dispatched" else None)


def test_log_limit_after_queue_receipt_is_partial_dispatch_not_an_unexecuted_action():
    data = recording()
    data[0].remove(event_at(data[0], "advance_ticks"))
    event_at(data[0], "run_end").update(detail="max_log_bytes")
    numbered(data[0])
    result = audit(data)
    assert result["turns"][0]["dispatch"]["state"] == "partially_dispatched"
    assert result["turns"][0]["queue"]["accepted"] is True
    assert result["verified"] is None


def test_absent_historical_queue_receipt_does_not_invent_rejection():
    data = recording()
    data[0].remove(event_at(data[0], "queue_brew"))
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["queue"]["accepted"] is None
    assert "queue_missing" in codes(result)


@pytest.mark.parametrize("field,value", [("requested_ticks", 16), ("elapsed_ticks", 16),
                                        ("start_absolute_tick", 99), ("absolute_tick", 118),
                                        ("absolute_tick", True), ("overshoot_ticks", 1)])
def test_native_advance_receipt_must_agree_with_args_and_snapshot_boundaries(field, value):
    data = recording(action="wait")
    event_at(data[0], "advance_ticks")["result"][field] = value
    assert audit(data)["verified"] is False


def test_advance_argument_mismatch_is_invalid_even_without_receipt():
    data = recording(action="wait")
    event_at(data[0], "advance_ticks").update(arguments={"ticks": 99}, result=None, error={"type": "TimeoutError"})
    result = audit(data)
    assert result["verified"] is False
    assert "advance_arguments_mismatch" in codes(result)


@pytest.mark.parametrize("missing", ["requested_ticks", "elapsed_ticks", "start_absolute_tick", "absolute_tick"])
def test_missing_advance_receipt_fields_remain_unknown(missing):
    data = recording(action="wait")
    event_at(data[0], "advance_ticks")["result"].pop(missing)
    result = audit(data)
    assert result["verified"] is None
    assert result["invalid_issue_count"] == 0


@pytest.mark.parametrize("paused,verified,state", [(False, False, "invalid"), (None, None, "attempted")])
def test_successful_advance_must_confirm_paused_boundary(paused, verified, state):
    data = recording(action="wait")
    event_at(data[0], "advance_ticks")["result"]["paused"] = paused
    result = audit(data)
    assert result["verified"] is verified
    assert result["turns"][0]["advance"]["state"] == state


@pytest.mark.parametrize("field,value", [("absolute_tick", 118), ("save_directory", "different-save")])
def test_snapshot_contradictions_are_not_hidden_by_a_valid_receipt(field, value):
    data = recording(action="wait")
    data[0][-2]["snapshot"][field] = value
    result = audit(data)
    assert result["verified"] is False
    assert result["turns"][0]["snapshot_alignment"]["verified"] is False


def test_next_turn_snapshot_cannot_replace_missing_post_action_snapshot():
    data = recording(action="wait")
    data[0].insert(-2, {"kind": "policy_input", "turn": 2})
    data[0][-2]["turn"] = 2
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["snapshot_alignment"]["after_tick"] is None
    assert "after_snapshot_missing" in codes(result)


def test_unknown_provider_is_not_guessed_from_response_shape():
    data = recording()
    data[1]["kind"] = "unknown-provider"
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["response"]["state"] == "unknown"
    assert result["turns"][0]["decision"]["agreement_verified"] is None


def test_empty_evidence_is_not_vacuously_verified():
    assert audit_action_evidence([], {}, {})["verified"] is None


def test_issue_limits_retain_totals_and_source_references(monkeypatch):
    monkeypatch.setattr(action_evidence, "MAX_ISSUES", 3)
    monkeypatch.setattr(action_evidence, "MAX_TURN_ISSUES", 2)
    data = recording()
    event_at(data[0], "queue_brew").update(arguments={}, result={})
    event_at(data[0], "advance_ticks").update(arguments={}, result={})
    result = audit(data)
    assert result["issue_count"] > len(result["issues"]) == 3
    assert result["omitted_issue_count"] == result["issue_count"] - 3
    turn = result["turns"][0]
    assert turn["issue_count"] > len(turn["issues"]) == 2
    assert turn["omitted_issue_count"] == turn["issue_count"] - 2
    assert turn["invalid_issue_count"] + turn["unknown_issue_count"] == turn["issue_count"]
    assert result["verified"] is False
    assert all(issue["event_ids"] and issue["event_indices"] for issue in result["issues"])


@pytest.mark.parametrize("field", ["workshop_id", "reaction", "completed"])
def test_missing_native_queue_contract_fields_prevent_full_verification(field):
    data = recording()
    event_at(data[0], "queue_brew")["result"].pop(field)
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["queue"]["accepted"] is None
    assert f"queue_{field}_missing" in codes(result)


@pytest.mark.parametrize("change,verified", [("missing", None), ("duplicate", False), ("out_of_order", False)])
def test_input_order_is_verified_separately_from_response_agreement(change, verified):
    data = recording()
    event = event_at(data[0], "policy_input")
    if change == "missing":
        data[0].remove(event)
    elif change == "duplicate":
        data[0].insert(data[0].index(event), copy.deepcopy(event))
    else:
        data[0].remove(event)
        data[0].insert(data[0].index(event_at(data[0], "decision")), event)
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is verified
    assert result["turns"][0]["input"]["verified"] is verified
    assert result["turns"][0]["decision"]["agreement_verified"] is True


def test_turn_limit_counts_distinct_omitted_turns_without_emitting_them(monkeypatch):
    monkeypatch.setattr(action_evidence, "MAX_TURNS", 1)
    data = recording()
    data[0].extend([{"kind": "policy_input", "turn": 2}, {"kind": "policy_response", "turn": 2},
                    {"kind": "policy_input", "turn": 3}])
    numbered(data[0])
    result = audit(data)
    assert len(result["turns"]) == 1
    assert result["turn_count"] == 3
    assert result["omitted_turn_count"] == 2
    assert result["verified"] is None


def test_failed_advance_remains_unknown_even_if_partial_result_says_unpaused():
    data = recording(action="wait")
    event_at(data[0], "advance_ticks")["result"]["paused"] = False
    event_at(data[0], "advance_ticks")["error"] = {"type": "TimeoutError", "message": "Pause not confirmed"}
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["advance"]["verified"] is None


def test_malformed_terminal_detail_does_not_crash_or_confirm_non_dispatch():
    data = recording(action="wait")
    data[0].remove(event_at(data[0], "advance_ticks"))
    event_at(data[0], "run_end")["detail"] = ["max_total_ticks"]
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is None
    assert result["turns"][0]["dispatch"]["state"] == "unknown"


@pytest.mark.parametrize("operation", ["dig", "run_command", "", 17, [], {}])
def test_unsupported_native_operation_cannot_hide_inside_verified_evidence(operation):
    data = recording(action="wait")
    data[0].insert(-1, {"kind": "action_result", "turn": 1, "operation": operation,
                        "arguments": {}, "result": {}})
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is False
    issue = next(issue for issue in result["issues"] if issue["code"] == "operation_unsupported")
    assert issue["severity"] == "invalid"
    assert issue["event_ids"] == [data[0][-2]["event"]]
    assert result["turns"][0]["event_counts"]["advance_ticks"] == 1


@pytest.mark.parametrize("explicit_null", [False, True])
def test_native_result_with_missing_operation_qualifies_the_audit(explicit_null):
    data = recording(action="wait")
    event = {"kind": "action_result", "turn": 1, "arguments": {}, "result": {}}
    if explicit_null:
        event["operation"] = None
    data[0].insert(-1, event)
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is None
    assert result["invalid_issue_count"] == 0
    assert "operation_missing" in codes(result)


@pytest.mark.parametrize("operation", ["status", "pause", "set_simulation_fps", "restore_simulation_fps", "observe"])
def test_runner_bookkeeping_is_allowed_without_becoming_an_extra_policy_action(operation):
    data = recording(action="wait")
    event = {"kind": "action_result", "turn": 1, "operation": operation,
             "arguments": {"fps": 100} if operation == "set_simulation_fps" else {}, "result": {}}
    if operation == "observe":
        event.update(result=None, error={"type": "LiveBridgeError"})
    data[0].insert(-1, event)
    numbered(data[0])
    result = audit(data)
    assert result["verified"] is True
    assert result["issues"] == []
    assert result["turn_count"] == 1
    assert result["turns"][0]["event_counts"] == {
        "policy_input": 1, "policy_response": 1, "decision": 1, "queue_brew": 0, "advance_ticks": 1}
