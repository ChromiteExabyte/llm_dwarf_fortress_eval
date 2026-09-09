"""Audit recorded actions without executing a policy, game call, or replay.

``False`` means contradictory evidence, not a finding of intentional tampering.
``None`` means the available records cannot establish the claim. In particular,
missing products never establish job completion, failure, or cancellation.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Any

from .policies import DecisionError, parse_decision, strict_json, validate_decision


MAX_TURN_ISSUES = 20
MAX_ISSUES = 1000
MAX_TURNS = 10000
MAX_RESPONSE_BYTES = 2_000_000
# Native protocol-v1 runner operations. Bookkeeping remains outside the policy
# action counts; an explicit operation outside this boundary cannot be ignored.
_RUNNER_OPERATIONS = frozenset({"status", "observe", "pause", "advance_ticks", "queue_brew",
                                "set_simulation_fps", "restore_simulation_fps"})
_PRE_DISPATCH_STOPS = {
    "max_total_ticks",
    "max_bridge_calls; action and following observation require more calls",
}


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _combined(*values: bool | None) -> bool | None:
    if any(value is False for value in values):
        return False
    return True if values and all(value is True for value in values) else None


class _Issues:
    def __init__(self, events: list[Any], report: dict[str, Any]):
        self.events = events
        self.report = report

    def add(self, target: dict[str, Any] | None, code: str, severity: str,
            message: str, indices: list[int] | tuple[int, ...] = ()) -> None:
        refs = list(indices[:8])
        issue = {"code": code, "severity": severity, "message": message,
                 "turn": target["turn"] if target is not None else None,
                 "event_ids": [self.events[index].get("event") for index in refs
                               if isinstance(self.events[index], dict)
                               and _integer(self.events[index].get("event"))],
                 "event_indices": refs}
        for bucket, cap in ((self.report, MAX_ISSUES), (target, MAX_TURN_ISSUES)):
            if bucket is None:
                continue
            bucket["issue_count"] += 1
            bucket[f"{severity}_issue_count"] += 1
            if len(bucket["issues"]) < cap:
                bucket["issues"].append(issue)
            else:
                bucket["omitted_issue_count"] += 1


def _counts() -> dict[str, Any]:
    return {"issues": [], "issue_count": 0, "invalid_issue_count": 0,
            "unknown_issue_count": 0, "omitted_issue_count": 0}


def _response(event: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Apply the recorded provider's acceptance rules to its original text."""
    result = {"state": "unknown", "decision": None, "verified": None}
    exchange = event.get("exchange")
    if isinstance(exchange, dict) and exchange.get("response_truncated") is True:
        return {**result, "state": "truncated", "verified": True}
    if event.get("error") is not None:
        return {**result, "state": "failed", "verified": True}
    if not isinstance(exchange, dict) or exchange.get("response_text") is None:
        return {**result, "state": "missing"}
    text = exchange["response_text"]
    if not isinstance(text, str):
        return {**result, "state": "failed", "verified": True}
    try:
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            return result  # Bounded audit, not a guessed provider failure.
        if policy.get("is_model") is False:
            decision = parse_decision(text)
        elif policy.get("is_model") is True and policy.get("kind") in ("ollama", "chat_completions"):
            body = strict_json(text)
            if not isinstance(body, dict):
                raise DecisionError("Provider envelope must be an object")
            if policy["kind"] == "ollama":
                if "error" in body:
                    raise DecisionError("Provider reported an error")
                finish = body.get("done_reason")
                if finish == "length":
                    return {**result, "state": "truncated", "verified": True}
                if body.get("done") is not True or finish != "stop":
                    raise DecisionError("Response did not finish successfully")
                message = body.get("message")
                tokens = body.get("eval_count")
            else:
                choices = body.get("choices")
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    raise DecisionError("Response must have exactly one choice")
                finish = choices[0].get("finish_reason")
                if finish == "length":
                    return {**result, "state": "truncated", "verified": True}
                if finish != "stop":
                    raise DecisionError("Response did not finish successfully")
                message = choices[0].get("message")
                usage = body.get("usage")
                tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            cap = exchange.get("reserved_output_tokens", policy.get("max_completion_tokens"))
            if _integer(tokens) and _integer(cap, 1) and tokens > cap:
                raise DecisionError("Provider exceeded reserved output tokens")
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or any(message.get(key) for key in ("tool_calls", "function_call", "refusal"))
                    or (policy["kind"] == "ollama" and message.get("images"))):
                raise DecisionError("Response is not a plain assistant message")
            decision = parse_decision(message.get("content"))
        else:
            return result
    except (DecisionError, UnicodeError, RecursionError):
        return {**result, "state": "failed", "verified": True}
    return {"state": "accepted", "decision": decision, "verified": True}


def audit_action_evidence(events: list[Any], policy_config: dict[str, Any],
                          config: dict[str, Any]) -> dict[str, Any]:
    """Link original records in encounter order; never repair or mutate them.

    Request/observation/prompt verification belongs to the model input contract.
    This audit establishes response agreement and action evidence independently
    of the model's definition of care. Receipt acceptance is not job completion.
    Reports retain bounded issues and event references, with full issue counts.
    Dispatch verification establishes a recorded call attempt; queue acceptance
    and exact advancement have separate verification fields.
    """
    report = {"schema_version": 1, "kind": "native_action_evidence_audit",
              "verified": None, "turns": [], "turn_count": 0,
              "omitted_turn_count": 0, **_counts()}
    if not isinstance(events, list):
        report.update(issue_count=1, unknown_issue_count=1,
                      issues=[{"code": "events_missing", "severity": "unknown",
                               "message": "An ordered event array is unavailable.", "turn": None,
                               "event_ids": [], "event_indices": []}])
        return report
    policy = policy_config if isinstance(policy_config, dict) else {}
    settings = config if isinstance(config, dict) else {}
    issues = _Issues(events, report)
    groups: dict[int, dict[str, list[int]]] = {}
    omitted_turns: set[int] = set()
    snapshots: list[int] = []
    boundaries: list[int] = []
    ends: list[int] = []
    previous_id = -1
    previous_turn = 0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            issues.add(None, "event_invalid", "invalid", "Event is not an object.", [index])
            continue
        event_id = event.get("event")
        if not _integer(event_id):
            issues.add(None, "event_id_missing", "unknown", "Event has no usable source ID.", [index])
        elif event_id <= previous_id:
            issues.add(None, "event_order", "invalid", "Source event IDs are not unique and increasing.", [index])
        else:
            previous_id = event_id
        kind = event.get("kind")
        if kind == "snapshot":
            snapshots.append(index)
        if kind == "run_end":
            ends.append(index)
        if kind == "action_result":
            operation = event.get("operation")
            if operation is None:
                issues.add(None, "operation_missing", "unknown",
                           "Native action result has no recorded operation.", [index])
            elif not isinstance(operation, str) or operation not in _RUNNER_OPERATIONS:
                issues.add(None, "operation_unsupported", "invalid",
                           "Native action result names an operation outside the bounded runner contract.", [index])
        category = kind if kind in ("policy_input", "policy_response", "decision") else None
        if kind == "action_result" and event.get("operation") in ("queue_brew", "advance_ticks"):
            category = event["operation"]
        if category is None:
            continue
        turn = event.get("turn")
        if not _integer(turn, 1):
            issues.add(None, "turn_missing", "unknown", "Action evidence has no usable positive turn.", [index])
            continue
        if turn < previous_turn:
            issues.add(None, "turn_order", "invalid", "Action evidence returns to an earlier turn.", [index])
        previous_turn = max(previous_turn, turn)
        if category in ("policy_input", "policy_response", "decision"):
            boundaries.append(index)
        if turn not in groups:
            if len(groups) >= MAX_TURNS:
                omitted_turns.add(turn)
                issues.add(None, "audit_turn_limit", "unknown", "Further turn records exceed the audit limit.", [index])
                continue
            groups[turn] = {key: [] for key in ("policy_input", "policy_response", "decision", "queue_brew", "advance_ticks")}
        groups[turn][category].append(index)

    report["turn_count"] = len(groups) + len(omitted_turns)
    report["omitted_turn_count"] = len(omitted_turns)
    used_job_ids: set[int] = set()
    for turn, group in groups.items():
        item = {"turn": turn, "event_ids": {}, "event_counts": {},
                "input": {"verified": None},
                "response": {"state": "missing", "decision": None, "verified": None},
                "decision": {"value": None, "schema_verified": None, "agreement_verified": None},
                "dispatch": {"state": "unknown", "verified": None},
                "queue": {"state": "not_applicable", "verified": True, "accepted": None, "job_ids": []},
                "advance": {"state": "not_applicable", "verified": True},
                "snapshot_alignment": {"verified": None, "before_tick": None, "after_tick": None},
                "verified": None, **_counts()}
        report["turns"].append(item)

        def add(code: str, severity: str, message: str, refs: list[int] | tuple[int, ...] = ()) -> None:
            issues.add(item, code, severity, message, refs)

        for category, indices in group.items():
            item["event_ids"][category] = [events[i]["event"] for i in indices[:8]
                                            if _integer(events[i].get("event"))]
            item["event_counts"][category] = len(indices)
            if len(indices) > 1:
                add(f"duplicate_{category}", "invalid", f"More than one {category} record belongs to this turn.", indices)
        response_ids, decision_ids = group["policy_response"], group["decision"]
        input_ids = group["policy_input"]
        queue_ids, advance_ids = group["queue_brew"], group["advance_ticks"]
        if not input_ids:
            add("policy_input_missing", "unknown", "This turn has no recorded policy input.", response_ids + decision_ids)
        elif len(input_ids) != 1:
            item["input"]["verified"] = False
        elif len(response_ids) == 1:
            if input_ids[0] >= response_ids[0]:
                item["input"]["verified"] = False
                add("policy_input_order", "invalid", "Policy input must precede its response.", input_ids + response_ids)
            else:
                item["input"]["verified"] = True
        decision_index = decision_ids[0] if len(decision_ids) == 1 else None
        if len(response_ids) == 1:
            item["response"] = _response(events[response_ids[0]], policy)
        elif len(response_ids) > 1:
            item["response"]["verified"] = False
        value = None
        if decision_index is not None:
            try:
                value = validate_decision(events[decision_index].get("decision"))
                item["decision"].update(value=value, schema_verified=True)
            except DecisionError:
                item["decision"]["schema_verified"] = False
                add("decision_schema", "invalid", "Recorded accepted decision violates the decision schema.", decision_ids)
        elif decision_ids:
            item["decision"].update(schema_verified=False, agreement_verified=False)

        if decision_ids and response_ids and (len(response_ids) != 1 or len(decision_ids) != 1):
            item["decision"]["agreement_verified"] = False
        elif value is not None:
            if not response_ids or item["response"]["state"] in ("missing", "unknown"):
                add("response_missing", "unknown", "Accepted decision cannot be linked to a usable recorded response.", decision_ids + response_ids)
            elif response_ids[0] >= decision_index:
                item["decision"]["agreement_verified"] = False
                add("response_order", "invalid", "Response must precede its accepted decision.", response_ids + decision_ids)
            elif item["response"]["state"] != "accepted":
                item["decision"]["agreement_verified"] = False
                add("failed_response_accepted", "invalid", "A failed or truncated response is followed by an accepted decision.", response_ids + decision_ids)
            elif value != item["response"]["decision"]:
                item["decision"]["agreement_verified"] = False
                add("response_decision_mismatch", "invalid", "Accepted decision differs from the response, including its public text.", response_ids + decision_ids)
            else:
                item["decision"]["agreement_verified"] = True
        elif not decision_ids and item["response"]["state"] == "accepted":
            add("accepted_decision_missing", "unknown", "Usable response has no recorded accepted decision.", response_ids)

        if value is None:
            if queue_ids or advance_ids:
                add("dispatch_decision_missing", "unknown", "Native dispatch cannot be linked to a unique valid decision.", decision_ids + queue_ids + advance_ids)
            item["dispatch"]["state"] = "no_accepted_decision" if not decision_ids else "unknown"
        else:
            action = value["action"]
            for index in queue_ids + advance_ids:
                if index <= decision_index:
                    add("dispatch_order", "invalid", "Native dispatch must follow its accepted decision.", [decision_index, index])
            if action != "brew" and queue_ids:
                add("unexpected_queue", "invalid", "This decision does not authorize a brewing call.", decision_ids + queue_ids)
            if action == "finish" and advance_ids:
                add("unexpected_advance", "invalid", "Finish does not authorize advancing the game.", decision_ids + advance_ids)
            if queue_ids and advance_ids and queue_ids[0] >= advance_ids[0]:
                add("queue_advance_order", "invalid", "Brewing must be queued before advancing this turn.", queue_ids + advance_ids)

            if action == "finish":
                item["dispatch"] = {"state": "finish", "verified": not (queue_ids or advance_ids)}
                item["snapshot_alignment"]["verified"] = True
            elif not queue_ids and not advance_ids:
                terminal = events[ends[0]] if len(ends) == 1 and ends[0] > decision_index else {}
                if (terminal.get("turn") == turn and terminal.get("outcome") == "budget_exhausted"
                        and isinstance(terminal.get("detail"), str)
                        and terminal.get("detail") in _PRE_DISPATCH_STOPS):
                    item["dispatch"] = {"state": "not_dispatched", "verified": True,
                                        "reason": terminal["detail"]}
                    item["snapshot_alignment"]["verified"] = True
                else:
                    add("dispatch_missing", "unknown", "No receipt or explicit pre-dispatch budget stop establishes whether the action ran.", decision_ids + ends)
            else:
                item["dispatch"] = {"state": "dispatched" if advance_ids else "partially_dispatched",
                                    "verified": True}

            if action == "brew" and item["dispatch"]["state"] != "not_dispatched":
                item["queue"] = _audit_queue(events, queue_ids, value, used_job_ids, add)
            if action != "finish" and item["dispatch"]["state"] != "not_dispatched":
                item["advance"], item["snapshot_alignment"] = _audit_advance(
                    events, advance_ids, decision_index, turn, settings, snapshots, boundaries, add)
                item["event_ids"]["before_snapshot"] = item["snapshot_alignment"].pop("before_event")
                item["event_ids"]["after_snapshot"] = item["snapshot_alignment"].pop("after_event")

        item["verified"] = _combined(item["input"]["verified"], item["decision"]["schema_verified"],
                                      item["decision"]["agreement_verified"],
                                      item["dispatch"]["verified"], item["queue"]["verified"],
                                      item["advance"]["verified"], item["snapshot_alignment"]["verified"])
        if item["invalid_issue_count"]:
            item["verified"] = False
        elif item["unknown_issue_count"] and item["verified"] is True:
            item["verified"] = None
    report["verified"] = _combined(*(item["verified"] for item in report["turns"]))
    if report["invalid_issue_count"]:
        report["verified"] = False
    elif report["unknown_issue_count"]:
        report["verified"] = None
    return report


def _audit_queue(events, indices, decision, used_job_ids, add):
    result = {"state": "missing", "verified": None, "accepted": None, "job_ids": []}
    if len(indices) != 1:
        if not indices:
            add("queue_missing", "unknown", "Brewing decision has no queue receipt.")
        else:
            result["verified"] = False
        return result
    event = events[indices[0]]
    result["state"] = "attempted"
    expected = {"workshop_id": decision["workshop_id"], "quantity": decision["quantity"]}
    arguments = event.get("arguments")
    args_ok = None
    if arguments is None:
        add("queue_arguments_missing", "unknown", "Queue arguments were not recorded.", indices)
    elif (not isinstance(arguments, dict) or set(arguments) != set(expected)
          or any(type(arguments[key]) is not int or arguments[key] != value for key, value in expected.items())):
        args_ok = False
        add("queue_arguments_mismatch", "invalid", "Queue arguments differ from the accepted brewing decision.", indices)
    else:
        args_ok = True
    receipt = event.get("result")
    if event.get("error") is not None or receipt is None:
        add("queue_effect_unknown", "unknown", "Failed or missing queue receipt does not establish native acceptance.", indices)
        result["verified"] = _combined(args_ok, None)
        return result
    if not isinstance(receipt, dict):
        add("queue_receipt_invalid", "invalid", "Queue receipt is not an object.", indices)
        result["verified"] = False
        return result
    checks = [args_ok]
    ids = receipt.get("job_ids")
    if ids is None:
        checks.append(None)
        add("queue_job_ids_missing", "unknown", "Native queue job IDs are unavailable.", indices)
    elif (not isinstance(ids, list) or len(ids) != decision["quantity"]
          or any(not _integer(job) for job in ids) or len(set(ids)) != len(ids)):
        checks.append(False)
        add("queue_job_ids_invalid", "invalid", "Queue job IDs must be unique integers matching the requested quantity.", indices)
    else:
        result["job_ids"] = list(ids)
        checks.append(True)
        if any(job in used_job_ids for job in ids):
            checks.append(False)
            add("queue_job_id_reused", "invalid", "A queued job ID was already reported by an earlier queue receipt.", indices)
        used_job_ids.update(ids)
    queued = receipt.get("queued_jobs")
    if queued is None:
        checks.append(None)
        add("queue_count_missing", "unknown", "Native accepted queue count is unavailable.", indices)
    elif type(queued) is not int or queued != decision["quantity"]:
        checks.append(False)
        add("queue_count_mismatch", "invalid", "Native queue count differs from the requested quantity.", indices)
    else:
        checks.append(True)
    for field, expected in (("workshop_id", decision["workshop_id"]),
                            ("reaction", "BREW_DRINK_FROM_PLANT"), ("completed", False)):
        if receipt.get(field) is None:
            checks.append(None)
            add(f"queue_{field}_missing", "unknown", f"Native queue receipt lacks {field}.", indices)
        elif type(receipt[field]) is not type(expected) or receipt[field] != expected:
            checks.append(False)
            add(f"queue_{field}_mismatch", "invalid", f"Native queue receipt contradicts the expected {field}.", indices)
    result["verified"] = _combined(*checks)
    if result["verified"] is True:
        result.update(state="accepted", accepted=True)
    elif result["verified"] is False:
        result["state"] = "invalid"
    return result


def _audit_advance(events, indices, decision_index, turn, config, snapshots, boundaries, add):
    result = {"state": "missing", "verified": None, "requested_ticks": None,
              "elapsed_ticks": None, "start_absolute_tick": None, "absolute_tick": None}
    alignment = {"verified": None, "before_tick": None, "after_tick": None,
                 "before_event": None, "after_event": None}
    if len(indices) != 1:
        if not indices:
            add("advance_missing", "unknown", "Decision has no advance receipt.")
        else:
            result["verified"] = False
        return result, alignment
    index = indices[0]
    event = events[index]
    result["state"] = "attempted"
    checks: list[bool | None] = []
    expected = config.get("ticks_per_decision")
    if not _integer(expected, 1) or expected > 12000:
        severity = "unknown" if expected is None else "invalid"
        checks.append(None if expected is None else False)
        expected = None
        add("advance_config_invalid" if severity == "invalid" else "advance_config_missing",
            severity, "A usable configured tick interval is unavailable.", indices)
    args = event.get("arguments")
    ticks = None
    if args is None:
        checks.append(None)
        add("advance_arguments_missing", "unknown", "Advance arguments were not recorded.", indices)
    elif (not isinstance(args, dict) or set(args) != {"ticks"}
          or not _integer(args.get("ticks"), 1) or args["ticks"] > 12000
          or (expected is not None and args["ticks"] != expected)):
        checks.append(False)
        add("advance_arguments_mismatch", "invalid", "Advance arguments do not match the configured tick interval.", indices)
    else:
        ticks = args["ticks"]
        checks.append(True)
    receipt = event.get("result")
    if event.get("error") is not None or receipt is None:
        add("advance_effect_unknown", "unknown", "Failed or missing advance receipt leaves native effects unknown.", indices)
        result["verified"] = _combined(*checks, None)
        return result, alignment
    if not isinstance(receipt, dict):
        add("advance_receipt_invalid", "invalid", "Advance receipt is not an object.", indices)
        result["verified"] = False
        return result, alignment
    for field in ("requested_ticks", "elapsed_ticks", "start_absolute_tick", "absolute_tick"):
        value = receipt.get(field)
        if value is None:
            checks.append(None)
            add(f"advance_{field}_missing", "unknown", f"Advance receipt lacks {field}.", indices)
        elif not _integer(value):
            checks.append(False)
            add(f"advance_{field}_invalid", "invalid", f"Advance receipt has invalid {field}.", indices)
        else:
            result[field] = value
            checks.append(True)
    requested, elapsed, start, end = (result[field] for field in
                                     ("requested_ticks", "elapsed_ticks", "start_absolute_tick", "absolute_tick"))
    for field, value in (("requested_ticks", requested), ("elapsed_ticks", elapsed)):
        if value is not None and ticks is not None and value != ticks:
            checks.append(False)
            add(f"advance_{field}_mismatch", "invalid", f"Advance {field} differs from dispatched ticks.", indices)
    if start is not None and end is not None and elapsed is not None and end - start != elapsed:
        checks.append(False)
        add("advance_tick_arithmetic", "invalid", "Advance endpoints do not agree with elapsed ticks.", indices)
    if receipt.get("overshoot_ticks") is not None and (type(receipt["overshoot_ticks"]) is not int or receipt["overshoot_ticks"] != 0):
        checks.append(False)
        add("advance_overshoot", "invalid", "Advance receipt reports overshoot or an invalid overshoot value.", indices)
    if receipt.get("paused") is not True:
        if receipt.get("paused") is None:
            checks.append(None)
            add("advance_pause_unknown", "unknown", "Advance receipt does not confirm the game paused at the boundary.", indices)
        else:
            checks.append(False)
            add("advance_pause_mismatch", "invalid", "Successful advance receipt contradicts the required paused boundary.", indices)

    before_pos = bisect_left(snapshots, decision_index) - 1
    after_pos = bisect_right(snapshots, index)
    next_boundary_pos = bisect_right(boundaries, index)
    next_boundary = boundaries[next_boundary_pos] if next_boundary_pos < len(boundaries) else len(events)
    before_index = snapshots[before_pos] if before_pos >= 0 else None
    after_index = snapshots[after_pos] if after_pos < len(snapshots) and snapshots[after_pos] < next_boundary else None
    if after_index is not None and events[after_index].get("turn") not in (None, turn):
        after_index = None
    samples = {}
    alignment_checks: list[bool | None] = []
    for side, sample_index in (("before", before_index), ("after", after_index)):
        sample = events[sample_index].get("snapshot") if sample_index is not None else None
        sample = sample if isinstance(sample, dict) else {}
        samples[side] = sample
        tick = sample.get("absolute_tick")
        if tick is None:
            alignment_checks.append(None)
            add(f"{side}_snapshot_missing", "unknown", f"A surrounding {side} snapshot tick is unavailable.", indices)
        elif not _integer(tick):
            alignment_checks.append(False)
            add(f"{side}_snapshot_invalid", "invalid", f"The {side} snapshot tick is invalid.", [sample_index])
        else:
            alignment[f"{side}_tick"] = tick
            alignment_checks.append(True)
        if sample_index is not None and _integer(events[sample_index].get("event")):
            alignment[f"{side}_event"] = events[sample_index]["event"]
        endpoint = start if side == "before" else end
        if _integer(tick) and endpoint is not None and tick != endpoint:
            alignment_checks.append(False)
            add(f"{side}_snapshot_mismatch", "invalid", f"Advance boundary differs from its {side} snapshot.", [sample_index, index])
    before, after = alignment["before_tick"], alignment["after_tick"]
    if before is not None and after is not None and ticks is not None and after - before != ticks:
        alignment_checks.append(False)
        add("snapshot_interval_mismatch", "invalid", "Surrounding snapshots do not span the dispatched tick interval.", [before_index, index, after_index])
    saves = [sample.get("save_directory") for sample in (samples["before"], receipt, samples["after"])]
    known_saves = [save for save in saves if isinstance(save, str)]
    if len(set(known_saves)) > 1:
        alignment_checks.append(False)
        add("snapshot_world_mismatch", "invalid", "Advance and surrounding snapshots name different saves.", indices)
    result["verified"] = _combined(*checks)
    result["state"] = "confirmed" if result["verified"] is True else "invalid" if result["verified"] is False else "attempted"
    alignment["verified"] = _combined(*alignment_checks, result["verified"])
    return result, alignment
