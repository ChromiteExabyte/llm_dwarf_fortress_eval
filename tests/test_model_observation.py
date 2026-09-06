import copy
import json
from pathlib import Path

import pytest

from dfeval.model_observation import (
    MAX_MODEL_OBSERVATION_BYTES, MODEL_OBSERVATION_VERSION, NEED_COLUMNS,
    STOCK_ITEM_COLUMNS, ModelObservationError, ModelObservationTooLarge,
    canonical_json_bytes, model_input_bytes, project_observation, projection_contract,
)
from dfeval.policies import json_bytes


def reverse_object_order(value):
    if isinstance(value, dict):
        return {key: reverse_object_order(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reverse_object_order(item) for item in value]
    return value


def test_actual_native_ab_start_has_identical_smaller_policy_bytes_across_sessions():
    # Recorded facts only: loading this fixture neither contacts DF nor a model.
    fixture = json.loads((Path(__file__).parent / "fixtures" /
                          "native-loop-initial-observations.json").read_text(encoding="utf-8-sig"))
    first, second = fixture["a"], fixture["b"]
    assert first["brewing"]["session"] != second["brewing"]["session"]
    assert json_bytes(first) != json_bytes(second)
    pristine = copy.deepcopy(first)
    projected = project_observation(first)
    assert model_input_bytes(first, []) == model_input_bytes(reverse_object_order(second), [])
    assert model_input_bytes(first, []) == model_input_bytes(projected, [])
    assert len(model_input_bytes(first, [])) < len(json_bytes({"observation": first, "history": []})) * .65
    assert first == pristine
    assert len(projected["citizens"]) == len(first["citizens"]) == 7
    for source, citizen in zip(first["citizens"], projected["citizens"]):
        assert {k: v for k, v in citizen.items() if k != "needs"} == {k: v for k, v in source.items() if k != "needs"}
        assert [dict(zip(NEED_COLUMNS, row)) for row in citizen["needs"]] == source["needs"]
    assert [dict(zip(STOCK_ITEM_COLUMNS, row)) for row in projected["stocks"]["items"]] == first["stocks"]["items"]


def test_bookkeeping_removed_but_care_resources_jobs_and_product_evidence_kept():
    event = {"id": 3, "session": "old", "epoch": 1, "kind": "native_reaction_product",
             "source": "dfhack.eventful.onReactionComplete", "reaction": "BREW_DRINK_FROM_PLANT",
             "absolute_tick": 500, "job_id": 9, "workshop_id": 2, "worker_id": 4,
             "item_next_id_before": 30, "item_next_id_after": 31,
             "outputs": [{"id": 30, "item_type": "DRINK", "stack_size": 25, "newly_created": True}],
             "errors": [], "dropped_outputs": 0}
    native = {"absolute_tick": 500, "citizens": [{"id": 4, "dead": False, "stress": None,
                "needs": [{"id": 1, "focus_level": None}], "current_job": {"id": 9}}],
              "known_former_citizens": [{"id": 5, "dead": None, "missing": True}],
              "stocks": {"items": [{"id": 30, "stack_size": None}], "path_accessibility": None,
                         "by_item_type": {"DRINK": {"stack_units": 25}}, "definitions": {"long": "boilerplate"}},
              "workshops": [{"id": 2, "completed": True, "type": "Still", "jobs": [{"id": 9}]}],
              "jobs": [{"id": 9, "completion_timer": 10}],
              "brewing": {"session": "old", "epoch": 1, "available": True, "events": [event],
                          "error_count": 0, "dropped_events": 0, "notes": "repeated explanation"},
              "simulation_fps": {"original": 100, "effective": 1000}, "ui_focus": ["random"],
              "paused": True, "df_version": "version", "save_directory": "site", "measurement_notes": {"long": "text"}}
    seen = project_observation(native)
    assert seen["citizens"] == native["citizens"]
    assert seen["known_former_citizens"] == native["known_former_citizens"]
    assert seen["stocks"] == {key: value for key, value in native["stocks"].items() if key != "definitions"}
    assert seen["jobs"] == native["jobs"] and seen["workshops"] == native["workshops"]
    assert seen["brewing"]["events"] == [{key: value for key, value in event.items() if key not in ("session", "epoch")}]
    assert seen["brewing"]["error_count"] == 0 and seen["brewing"]["dropped_events"] == 0
    assert not {"paused", "ui_focus", "simulation_fps", "df_version", "save_directory", "measurement_notes"} & seen.keys()
    native["brewing"].update(session="another", epoch=8, notes="new explanatory text")
    native["brewing"]["events"][0].update(session="another", epoch=8)
    native.update(simulation_fps={"effective": 50}, ui_focus=["another"], paused=False)
    assert model_input_bytes(native, []) == model_input_bytes(seen, [])
    native["brewing"]["events"][0]["outputs"][0]["stack_size"] = 24
    assert model_input_bytes(native, []) != model_input_bytes(seen, [])


def test_unknown_absent_empty_and_extended_measurements_are_not_filled_or_discarded():
    native = {"citizens": [{"id": 1, "needs": None}, {"id": 2, "needs": []},
                           {"id": 3}, {"id": 4, "needs": [{"id": 1, "focus_level": None}]}],
              "stocks": None, "jobs": None, "brewing": None, "errors": None}
    assert project_observation(native)["citizens"] == native["citizens"]
    assert "known_former_citizens" not in project_observation(native)
    assert all(project_observation(native)[key] is None for key in ("stocks", "jobs", "brewing", "errors"))
    row = dict(zip(NEED_COLUMNS, [1, "DrinkAlcohol", -90, 10, -1]))
    row["future_measurement"] = 42
    assert project_observation({"citizens": [{"needs": [row]}]})["citizens"][0]["needs"] == [row]


def test_projection_is_independent_idempotent_and_preserves_native_numbers_and_history_order():
    source = {"citizens": [{"id": 1, "stress": 1.0}], "absolute_tick": 1}
    seen = project_observation(source)
    assert project_observation(seen) == seen
    assert b'"stress":1.0' in canonical_json_bytes(seen)
    seen["citizens"][0]["stress"] = 50
    assert source["citizens"][0]["stress"] == 1.0
    history = [{"notebook": "first", "action": "wait", "reason": "x"},
               {"action": "finish", "reason": "y", "notebook": "second"}]
    assert model_input_bytes(source, history) == model_input_bytes(reverse_object_order(source), reverse_object_order(history))
    assert model_input_bytes(source, history) != model_input_bytes(source, list(reversed(history)))


def test_projection_caps_complete_bytes_without_silently_dropping_citizens():
    native = {"citizens": [{"id": 1, "name": "x" * MAX_MODEL_OBSERVATION_BYTES}]}
    with pytest.raises(ModelObservationTooLarge, match="not sent"):
        project_observation(native)
    small = project_observation({"citizens": [{"name": "á"}]})
    size = len(canonical_json_bytes(small))
    assert project_observation(small, max_bytes=size) == small
    with pytest.raises(ModelObservationTooLarge):
        project_observation(small, max_bytes=size - 1)
    assert projection_contract()["max_observation_bytes"] == MAX_MODEL_OBSERVATION_BYTES


@pytest.mark.parametrize("bad", [None, [], {"citizens": [{"stress": float("nan")}]},
                                  {"citizens": [{"name": "\ud800"}]},
                                  {"model_observation_version": "other"},
                                  {"model_observation_version": MODEL_OBSERVATION_VERSION, "table_columns": {}}])
def test_invalid_json_or_projection_declaration_is_rejected(bad):
    with pytest.raises(ModelObservationError):
        project_observation(bad)
