import copy

from dfeval.care import distribution, summarize_events, summarize_run, summarize_snapshot


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
