import copy

import pytest

from dfeval.care import distribution, summarize_brewing, summarize_events, summarize_run, summarize_snapshot


def test_raw_distributions_preserve_missingness_and_negative_stress():
    value = distribution([-100, None, 50, True, float("nan")])
    assert value == {"measured": 2, "unknown": 3, "min": -100, "median": -25,
                     "mean": -25, "max": 50}
    assert distribution([])["mean"] is None


def test_missing_is_distinct_from_zero_population_and_drinks():
    unknown = summarize_snapshot({})
    assert unknown["population"] is None
    assert unknown["drink"]["stack_units"] is None
    assert unknown["citizen_measurements"]["stress"] is None
    assert summarize_run([{}])["observed_confirmed_deaths"] is None
    measured = summarize_snapshot({"citizens": [], "stocks": {"by_item_type": {
        "DRINK": {"stack_units": 0, "item_objects": 0}}}})
    assert measured["population"] == 0
    assert measured["drink"] == {"stack_units": 0, "item_objects": 0, "candidate_stack_units": None}


def test_needs_are_grouped_in_raw_native_units_without_weights():
    summary = summarize_snapshot({"citizens": [
        {"id": 1, "stress": -900, "thirst_timer": 12, "needs": [
            {"type": "DrinkAlcohol", "focus_level": -123, "need_level": 4}]},
        {"id": 2, "stress": None, "thirst_timer": None, "needs": None},
    ]})
    assert summary["needs"]["DrinkAlcohol"]["focus_level"]["mean"] == -123
    assert summary["citizens_with_unknown_needs"] == 1
    assert summary["citizen_measurements"]["stress"]["unknown"] == 1
    assert "score" not in summary


def test_missing_citizens_are_not_deaths_and_confirmed_deaths_deduplicate():
    first = {"absolute_tick": 100, "citizens": [{"id": 1}, {"id": 2}],
             "known_former_citizens": [], "stocks": {"by_item_type": {"DRINK": {"stack_units": 5}}}}
    missing = {"absolute_tick": 200, "citizens": [{"id": 1}],
               "known_former_citizens": [{"id": 2, "dead": None, "missing": True}]}
    dead = copy.deepcopy(missing)
    dead["known_former_citizens"] = [{"id": 2, "dead": True}]
    assert summarize_run([first, missing])["observed_confirmed_deaths"] == 0
    result = summarize_run([first, missing, dead, dead])
    assert result["observed_confirmed_deaths"] == 1
    assert result["observed_confirmed_dead_ids"] == [2]
    assert result["drink_stack_units_change"] is None
    assert result["population_change"] == -1
    assert result["observed_tick_span"] == 100


def test_event_replay_recomputes_identical_summary_without_mutating_snapshots():
    snapshots = [{"absolute_tick": 1, "citizens": []}, {"absolute_tick": 3, "citizens": []}]
    original = copy.deepcopy(snapshots)
    events = [{"kind": "snapshot", "snapshot": s} for s in snapshots]
    events.insert(1, {"kind": "decision", "decision": {"action": "wait"}})
    assert summarize_events(events) == summarize_run(snapshots)
    assert snapshots == original
    assert summarize_run([])["initial"] is None
    assert summarize_run([])["observed_confirmed_deaths"] is None


def test_explicit_dead_records_do_not_inflate_living_population_or_stress():
    summary = summarize_snapshot({"citizens": [
        {"id": 1, "dead": False, "stress": 10},
        {"id": 2, "dead": True, "stress": 1000},
    ]})
    assert summary["population"] == 1
    assert summary["confirmed_dead_ids"] == [2]
    assert summary["citizen_measurements"]["stress"]["mean"] == 10


def _product(event_id=1, item_id=100, units=25, job_id=7, workshop_id=3, tick=110):
    return {"id": event_id, "session": "recorded-session", "epoch": 1,
            "source": "dfhack.eventful.onReactionComplete", "kind": "native_reaction_product",
            "reaction": "BREW_DRINK_FROM_PLANT", "job_id": job_id, "workshop_id": workshop_id,
            "worker_id": 9, "absolute_tick": tick, "item_next_id_before": item_id,
            "item_next_id_after": item_id + 1, "dropped_outputs": 0, "errors": [],
            "outputs": [{"id": item_id, "item_type": "DRINK", "stack_size": units, "newly_created": True}]}


def _sample(products=(), tick=120, lost=0):
    return {"absolute_tick": tick, "citizens": [], "brewing": {
        "available": True, "session": "recorded-session", "epoch": 1,
        "event_count": len(products) + lost, "dropped_events": lost, "error_count": 0,
        "events": copy.deepcopy(list(products)), "queued_jobs": 1 if products else 0}}


def _ledger():
    # Only the minimal chronological evidence is required. An end marker and
    # unrelated provider/bridge requests are not necessary for receipt linkage.
    return [{"kind": "run_start"}, {"kind": "snapshot", "snapshot": _sample(tick=100)},
            {"kind": "action_result", "operation": "queue_brew",
             "arguments": {"workshop_id": 3, "quantity": 1},
             "result": {"workshop_id": 3, "job_ids": [7], "queued_jobs": 1,
                        "reaction": "BREW_DRINK_FROM_PLANT", "completed": False}},
            {"kind": "snapshot", "snapshot": _sample([_product()])}]


def test_snapshot_products_require_explicitly_unknown_receipt_linkage():
    summary = summarize_brewing([_sample([_product()])])
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["product_evidence_complete"] is True
    assert summary["receipt_linkage"] == "unknown"
    assert summary["unlinked_product_jobs"] is None
    assert summary["receipt_linkage_errors"] is None
    assert summary["evidence_complete"] is False and summary["qualified_total"] is True


def test_minimal_chronological_run_links_products_to_native_queue_receipts():
    events = _ledger()
    original = copy.deepcopy(events)
    summary = summarize_events(iter(events))["brewing"]
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["jobs_with_confirmed_drink_products"] == 1
    assert summary["receipt_linkage"] == "verified" and summary["unlinked_product_jobs"] == 0
    assert summary["product_evidence_complete"] is summary["evidence_complete"] is True
    assert summary["qualified_total"] is False and events == original
    assert summary["unknown_queue_receipts"] == summary["product_jobs_with_unknown_receipt"] == 0


@pytest.mark.parametrize("section,field", [
    ("result", "workshop_id"), ("result", "job_ids"), ("result", "queued_jobs"),
    ("result", "reaction"), ("result", "completed"),
    ("arguments", "workshop_id"), ("arguments", "quantity"),
])
@pytest.mark.parametrize("null", [False, True])
def test_missing_receipt_fields_are_unknown_and_preserve_recorded_quantities(section, field, null):
    events = _ledger()
    if null:
        events[2][section][field] = None
    else:
        del events[2][section][field]
    original = copy.deepcopy(events)
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "unknown"
    assert summary["unknown_queue_receipts"] == summary["product_jobs_with_unknown_receipt"] == 1
    assert summary["receipt_linkage_errors"] == summary["unlinked_product_jobs"] == 0
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["product_evidence_complete"] is True and summary["evidence_complete"] is False
    assert summary["qualified_total"] is True and events == original


@pytest.mark.parametrize("section", ["result", "arguments"])
def test_missing_receipt_object_is_unknown_instead_of_an_absent_receipt_claim(section):
    events = _ledger()
    del events[2][section]
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "unknown"
    assert summary["unknown_queue_receipts"] == summary["product_jobs_with_unknown_receipt"] == 1
    assert summary["receipt_linkage_errors"] == 0
    assert summary["confirmed_new_drink_stack_units"] == 25


@pytest.mark.parametrize("field", ["session", "epoch"])
def test_missing_preceding_observation_owner_cannot_prove_an_absent_receipt(field):
    events = _ledger()
    del events[1]["snapshot"]["brewing"][field]
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "unknown"
    assert summary["unknown_queue_receipts"] == summary["product_jobs_with_unknown_receipt"] == 1
    assert summary["receipt_linkage_errors"] == summary["unlinked_product_jobs"] == 0
    # The incomplete earlier brewing sample independently qualifies intrinsic
    # product evidence; the later native item measurement is still retained.
    assert summary["confirmed_drink_products"][0]["stack_units"] == 25
    assert summary["evidence_complete"] is False


def test_missing_preceding_snapshot_leaves_receipt_owner_and_clock_unknown():
    events = _ledger()
    del events[1]
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "unknown" and summary["receipt_linkage_errors"] == 0
    assert summary["unknown_queue_receipts"] == summary["product_jobs_with_unknown_receipt"] == 1
    assert summary["confirmed_new_drink_stack_units"] == 25


@pytest.mark.parametrize("null", [False, True])
def test_missing_queue_snapshot_clock_cannot_verify_the_product_lower_time_bound(null):
    events = _ledger()
    if null:
        events[1]["snapshot"]["absolute_tick"] = None
    else:
        del events[1]["snapshot"]["absolute_tick"]
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "unknown"
    assert summary["unknown_queue_receipts"] == summary["product_jobs_with_unknown_receipt"] == 1
    assert summary["receipt_linkage_errors"] == 0
    assert summary["product_clock_verification"] == "verified"
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["evidence_complete"] is False and summary["qualified_total"] is True


@pytest.mark.parametrize("section,field,value", [
    ("result", "workshop_id", -1), ("result", "job_ids", []),
    ("result", "queued_jobs", True), ("result", "reaction", "OTHER"),
    ("result", "completed", True), ("arguments", "workshop_id", "3"),
    ("arguments", "quantity", 11),
])
def test_present_invalid_receipt_fields_are_contradictions_even_when_another_field_is_missing(section, field, value):
    events = _ledger()
    del events[2]["result"]["reaction" if field != "reaction" else "completed"]
    events[2][section][field] = value
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "inconsistent"
    assert summary["receipt_linkage_errors"] >= 1
    assert summary["confirmed_new_drink_stack_units"] is None


@pytest.mark.parametrize("change", ["different_job", "different_workshop", "late", "earlier_product"])
def test_incomplete_receipt_cannot_override_known_identifier_or_time_contradictions(change):
    events = _ledger()
    if change == "different_job":
        del events[2]["result"]["reaction"]
        events[2]["result"]["job_ids"] = [8]
    else:
        del events[2]["result"]["job_ids"]
        if change == "different_workshop":
            events[2]["result"]["workshop_id"] = events[2]["arguments"]["workshop_id"] = 4
        elif change == "late":
            events.append(events.pop(2))
        else:
            events[-1]["snapshot"]["brewing"]["events"][0]["absolute_tick"] = 90
    summary = summarize_events(events)["brewing"]
    assert summary["unknown_queue_receipts"] == 1
    assert summary["receipt_linkage"] == "inconsistent" and summary["unlinked_product_jobs"] == 1
    assert summary["confirmed_new_drink_stack_units"] is None


@pytest.mark.parametrize("clock", [-1, True, "100"])
def test_present_invalid_queue_clock_is_not_treated_as_missing(clock):
    events = _ledger()
    events[1]["snapshot"]["absolute_tick"] = clock
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "inconsistent" and summary["receipt_linkage_errors"] >= 1
    assert summary["confirmed_new_drink_stack_units"] is None


def test_cumulative_copies_deduplicate_but_changed_callback_never_wins():
    first = _sample([_product()])
    changed = copy.deepcopy(first)
    changed["brewing"]["events"][0]["outputs"][0]["stack_size"] = 999
    repeated = summarize_brewing([first, copy.deepcopy(first)])
    assert repeated["confirmed_new_drink_stack_units"] == 25
    assert repeated["recorded_product_events"] == 1
    for samples in ([first, changed], [changed, first]):
        summary = summarize_brewing(samples)
        assert summary["conflicting_product_events"] == 1
        assert summary["confirmed_new_drink_stack_units"] is None
        assert summary["confirmed_drink_products"] == []
        assert summary["product_evidence_complete"] is summary["evidence_complete"] is False


@pytest.mark.parametrize("change", ["quantity", "job"])
def test_conflicting_item_quantity_or_attribution_is_not_resolved_by_first_wins(change):
    first, second = _product(), _product(event_id=2)
    if change == "quantity":
        second["outputs"][0]["stack_size"] = 40
    else:
        second["job_id"] = 8
    summary = summarize_brewing([_sample([first, second])])
    assert summary["conflicting_product_items"] == 1
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["confirmed_drink_products"] == [
        {"session": "recorded-session", "epoch": 1, "id": 100, "stack_units": None}]
    assert summary["product_evidence_complete"] is False


def test_identical_item_references_within_one_callback_count_once():
    product = _product()
    product["outputs"].append(copy.deepcopy(product["outputs"][0]))
    summary = summarize_brewing([_sample([product])])
    assert summary["conflicting_product_items"] == 0
    assert summary["confirmed_drink_product_items"] == 1
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["product_evidence_complete"] is True


def test_distinct_callbacks_cannot_both_create_the_same_item_even_with_identical_quantities():
    summary = summarize_brewing([_sample([_product(), _product(event_id=2)])])
    assert summary["conflicting_product_items"] == 1
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["confirmed_drink_products"][0]["stack_units"] is None
    assert summary["product_evidence_complete"] is False


def test_newly_observed_callback_cannot_claim_a_future_native_tick():
    events = _ledger()
    events[-1]["snapshot"]["brewing"]["events"][0]["absolute_tick"] = 121
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "verified"  # the lower queue bound is satisfied
    assert summary["product_clock_verification"] == "inconsistent"
    assert summary["product_clock_errors"] == 1
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["confirmed_drink_products"][0]["stack_units"] == 25
    assert summary["product_evidence_complete"] is summary["evidence_complete"] is False


def test_later_snapshot_cannot_repair_an_impossible_first_callback_time():
    first = _sample([_product(tick=130)], tick=120)
    later = _sample([_product(tick=130)], tick=140)
    summary = summarize_brewing([first, later])
    assert summary["product_clock_errors"] == 1
    assert summary["confirmed_new_drink_stack_units"] is None


@pytest.mark.parametrize("clock", [None, True, -1, "120"])
def test_missing_or_invalid_containing_clock_qualifies_without_erasing_native_quantities(clock):
    sample = _sample([_product()])
    sample["absolute_tick"] = clock
    summary = summarize_brewing([sample])
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["product_clock_verification"] == "unknown"
    assert summary["product_events_with_unknown_snapshot_tick"] == 1
    assert summary["product_evidence_complete"] is summary["evidence_complete"] is False
    assert summary["qualified_total"] is True


def test_missing_first_containing_clock_is_not_filled_from_a_later_snapshot():
    first = {"brewing": _sample([_product()])["brewing"]}
    later = _sample([_product()], tick=150)
    summary = summarize_brewing([first, later])
    assert summary["product_clock_verification"] == "unknown"
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["qualified_total"] is True


@pytest.mark.parametrize("change", ["missing_count", "larger_count", "false_drop", "negative_drop", "duplicate_id", "gap"])
def test_inconsistent_event_counters_cannot_claim_complete_production(change):
    sample = _sample([_product()])
    evidence = sample["brewing"]
    if change == "missing_count":
        del evidence["event_count"]
    elif change == "larger_count":
        evidence["event_count"] = 4
    elif change == "false_drop":
        evidence["dropped_events"] = 1
    elif change == "negative_drop":
        evidence["dropped_events"] = -1
    elif change == "duplicate_id":
        evidence["events"].append(copy.deepcopy(evidence["events"][0]))
        evidence["event_count"] = 2
    else:
        evidence["events"][0]["id"] = 2
    summary = summarize_brewing([sample])
    assert summary["counter_inconsistencies"] >= 1
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["product_evidence_complete"] is False


def test_native_tail_drops_are_valid_counters_but_not_complete_production():
    sample = _sample([_product()], lost=2)
    summary = summarize_brewing([sample])
    assert summary["counter_inconsistencies"] == 0 and summary["dropped_events"] == 2
    assert summary["product_evidence_complete"] is False
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["confirmed_drink_products"][0]["stack_units"] == 25


def test_cumulative_history_and_drop_counters_cannot_regress_within_epoch():
    full = _sample([_product(), _product(2, 101)], lost=1)
    shorter = _sample([_product()], lost=2)  # same total, but a retained callback vanished
    assert summarize_brewing([full, shorter])["counter_inconsistencies"] == 1
    no_drop = _sample([_product(), _product(2, 101)])
    assert summarize_brewing([full, no_drop])["counter_inconsistencies"] == 1


@pytest.mark.parametrize("change", ["wrong_job", "wrong_workshop", "late_receipt", "failed_queue", "missing_receipt",
                                     "wrong_quantity", "wrong_reaction", "duplicate_receipt", "wrong_epoch", "earlier_tick"])
def test_product_linkage_rejects_missing_conflicting_or_nonprior_queue_receipts(change):
    events = _ledger()
    receipt = events[2]
    if change == "wrong_job":
        receipt["result"]["job_ids"] = [8]
    elif change == "wrong_workshop":
        receipt["arguments"]["workshop_id"] = receipt["result"]["workshop_id"] = 4
    elif change == "late_receipt":
        events.append(events.pop(2))
    elif change == "failed_queue":
        receipt["error"] = {"type": "LiveBridgeError", "message": "Queue rejected"}
    elif change == "missing_receipt":
        del events[2]
    elif change == "wrong_quantity":
        receipt["result"]["queued_jobs"] = 2
    elif change == "wrong_reaction":
        receipt["result"]["reaction"] = "OTHER"
    elif change == "duplicate_receipt":
        events.insert(3, copy.deepcopy(receipt))
    elif change == "wrong_epoch":
        events[-1]["snapshot"]["brewing"]["epoch"] = 2
        events[-1]["snapshot"]["brewing"]["events"][0]["epoch"] = 2
    else:
        events[-1]["snapshot"]["brewing"]["events"][0]["absolute_tick"] = 90
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "inconsistent"
    assert summary["receipt_linkage_errors"] >= 1
    assert summary["confirmed_new_drink_stack_units"] is None
    assert summary["confirmed_drink_products"][0]["stack_units"] == 25
    assert summary["evidence_complete"] is False


def test_snapshot_excerpt_cannot_invent_a_complete_queue_ledger():
    events = _ledger()[1:]
    summary = summarize_events(events)["brewing"]
    assert summary["receipt_linkage"] == "unknown"
    assert summary["confirmed_new_drink_stack_units"] == 25
    assert summary["evidence_complete"] is False


def test_products_without_queue_calls_are_inconsistent_even_when_native_shape_looks_valid():
    summary = summarize_events([{"kind": "run_start"},
                                {"kind": "snapshot", "snapshot": _sample([_product()])}])["brewing"]
    assert summary["unlinked_product_jobs"] == 1 and summary["receipt_linkage"] == "inconsistent"
    assert summary["confirmed_new_drink_stack_units"] is None


def test_idle_native_ledger_can_confirm_no_queued_products_without_a_target_score():
    summary = summarize_events([{"kind": "run_start"},
                                {"kind": "snapshot", "snapshot": _sample()}])["brewing"]
    assert summary["evidence_complete"] is True
    assert summary["confirmed_new_drink_stack_units"] == 0
    assert "score" not in summary
