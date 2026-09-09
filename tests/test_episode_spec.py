"""Episode declarations are data, independent of model bindings and live games."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from dfeval.episode_spec import (
    DEFAULT_OBJECTIVE, MAX_OBJECTIVE_BYTES, MAX_SPEC_BYTES, EpisodeSpec,
    EpisodeSpecError, load_spec, read_spec,
)


def specification(**changes):
    values = {
        "starting_checkpoint_sha256": "12" * 32,
        "information_condition": "player",
        "control_disclosure": "documented",
    }
    return EpisodeSpec(**(values | changes))


def test_default_objective_and_declarations_are_explicit():
    spec = specification()
    assert spec.objective == DEFAULT_OBJECTIVE == "Care for the dwarves"
    assert spec.to_dict() == {
        "schema_version": 1,
        "objective": "Care for the dwarves",
        "starting_checkpoint_sha256": "12" * 32,
        "starting_state": "undeveloped_embark",
        "declaration_basis": "operator_declared_unverified",
        "information_condition": "player",
        "control_disclosure": "documented",
        "memory_writes": "unresolved",
        "pause_control": "model",
    }
    with pytest.raises(TypeError):
        EpisodeSpec(starting_checkpoint_sha256="12" * 32)


def test_value_is_immutable_and_exported_object_is_independent():
    spec = specification()
    before = spec.sha256()
    with pytest.raises(FrozenInstanceError):
        spec.objective = "Changed"
    exported = spec.to_dict()
    exported["objective"] = "Changed"
    exported["history"] = ["Inherited strategy"]
    assert spec.objective == DEFAULT_OBJECTIVE
    assert spec.sha256() == before
    assert "history" not in spec.to_dict()


def test_canonical_hash_is_order_independent_but_preserves_exact_objective():
    spec = specification(objective="  Care for the dwarves — 矮人\n")
    differently_formatted = json.dumps(dict(reversed(list(spec.to_dict().items()))), indent=2)
    restored = load_spec(differently_formatted)
    assert restored == spec
    expected = json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    assert restored.canonical_bytes() == expected
    assert restored.to_json().encode("utf-8") == expected
    assert restored.sha256() == hashlib.sha256(expected).hexdigest()
    assert not expected.endswith(b"\n")
    assert replace(spec, objective=spec.objective.strip()).sha256() != spec.sha256()
    assert replace(spec, starting_checkpoint_sha256="34" * 32).sha256() != spec.sha256()


def test_maximum_objective_round_trips_even_when_json_escaping_expands_it():
    spec = specification(objective="x" + "\x00" * (MAX_OBJECTIVE_BYTES - 1))
    assert len(spec.canonical_bytes()) < MAX_SPEC_BYTES
    assert load_spec(spec.canonical_bytes()) == spec


@pytest.mark.parametrize("information", ["player", "privileged"])
@pytest.mark.parametrize("disclosure", ["documented", "undocumented"])
@pytest.mark.parametrize("writes", ["unresolved", "denied", "allowed"])
def test_conditions_round_trip_and_unresolved_live_prerequisite(information, disclosure, writes):
    spec = specification(information_condition=information,
                         control_disclosure=disclosure, memory_writes=writes)
    assert load_spec(spec.canonical_bytes()) == spec
    if writes == "unresolved":
        with pytest.raises(EpisodeSpecError, match="before a live episode"):
            spec.require_resolved_memory_writes()
    else:
        assert spec.require_resolved_memory_writes() is None
    # Reading or recording metadata does not require an evaluated game session.
    metadata = {"isolation": "none", "game_connected": False, "episode_spec": spec.to_dict()}
    assert EpisodeSpec.from_dict(metadata["episode_spec"]) == spec


def test_same_spec_can_bind_independent_models_without_inheriting_run_history(tmp_path):
    spec = specification()
    first = {
        "episode_spec": spec.to_dict(), "episode_spec_sha256": spec.sha256(),
        "model": "arbitrary-model-a", "session_directory": str(tmp_path / "a"),
        "resource_limits": {"max_calls": 4}, "history": [],
    }
    second = {
        "episode_spec": spec.to_dict(), "episode_spec_sha256": spec.sha256(),
        "model": "arbitrary-model-b", "session_directory": str(tmp_path / "b"),
        "resource_limits": {"max_calls": 7}, "history": [],
    }
    first["history"].append("A strategy chosen in the first session")
    first["resource_limits"]["max_calls"] = 2
    first["episode_spec"]["objective"] = "A changed declaration"
    assert second["history"] == []
    assert second["resource_limits"] == {"max_calls": 7}
    assert second["session_directory"] != first["session_directory"]
    assert second["episode_spec_sha256"] == first["episode_spec_sha256"] == spec.sha256()
    assert EpisodeSpec.from_dict(second["episode_spec"]) == spec
    assert set(spec.to_dict()).isdisjoint({"model", "provider", "resource_limits", "history", "session_directory"})


@pytest.mark.parametrize("field,value", [
    ("starting_checkpoint_sha256", "a" * 63),
    ("starting_checkpoint_sha256", "A" * 64),
    ("starting_checkpoint_sha256", "g" * 64),
    ("starting_checkpoint_sha256", None),
    ("information_condition", "automatic"),
    ("information_condition", ["player"]),
    ("control_disclosure", None),
    ("memory_writes", True),
    ("objective", "\t\n "),
    ("objective", [DEFAULT_OBJECTIVE]),
    ("objective", "\ud800"),
    ("objective", "a" * (MAX_OBJECTIVE_BYTES + 1)),
    ("objective", "矮" * (MAX_OBJECTIVE_BYTES // 3 + 1)),
])
def test_constructor_and_loader_reject_invalid_variable_fields(field, value):
    with pytest.raises(EpisodeSpecError):
        specification(**{field: value})
    recorded = specification().to_dict() | {field: value}
    with pytest.raises(EpisodeSpecError):
        load_spec(json.dumps(recorded))


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("starting_state", "prepared_fortress"),
    ("declaration_basis", "native_verified"),
    ("pause_control", "host"),
])
def test_fixed_declarations_cannot_silently_change(field, value):
    with pytest.raises(EpisodeSpecError, match="Unsupported"):
        load_spec(json.dumps(specification().to_dict() | {field: value}))


@pytest.mark.parametrize("field", list(specification().to_dict()))
def test_recorded_missing_fields_have_no_invented_defaults(field):
    recorded = specification().to_dict()
    del recorded[field]
    with pytest.raises(EpisodeSpecError, match="missing or unknown"):
        load_spec(json.dumps(recorded))


@pytest.mark.parametrize("field", [
    "model", "provider", "resource_limits", "history", "required_journal",
    "persona", "care_rubric", "helper_template", "session_directory",
])
def test_runtime_bindings_and_scaffold_fields_do_not_belong_in_spec(field):
    with pytest.raises(EpisodeSpecError, match="missing or unknown"):
        load_spec(json.dumps(specification().to_dict() | {field: None}))


@pytest.mark.parametrize("payload", [
    b"\xff", "{", "[]", "null", "true", '"text"',
    '{"schema_version":1,"schema_version":1}',
    '{"unknown":{"x":1,"x":2}}',
    '{"schema_version":NaN}', '{"schema_version":Infinity}',
    "[" * 1200 + "]" * 1200,
])
def test_loader_rejects_malformed_non_object_and_ambiguous_json(payload):
    with pytest.raises(EpisodeSpecError):
        load_spec(payload)


def test_input_and_file_reads_are_bounded_and_do_not_modify_source(tmp_path):
    spec = specification(objective="矮" * (MAX_OBJECTIVE_BYTES // 3))
    path = tmp_path / "episode.json"
    path.write_bytes(spec.canonical_bytes())
    original = path.read_bytes()
    assert read_spec(path) == spec
    assert path.read_bytes() == original
    for payload in (b" " * (MAX_SPEC_BYTES + 1), "矮" * (MAX_SPEC_BYTES // 3 + 1)):
        with pytest.raises(EpisodeSpecError, match="byte limit"):
            load_spec(payload)
    path.write_bytes(b" " * (MAX_SPEC_BYTES + 1))
    with pytest.raises(EpisodeSpecError, match="byte limit"):
        read_spec(path)
    with pytest.raises(EpisodeSpecError, match="regular file"):
        read_spec(tmp_path)
    with pytest.raises(EpisodeSpecError, match="Cannot read"):
        read_spec(tmp_path / "missing.json")
    with pytest.raises(EpisodeSpecError, match="str or bytes"):
        load_spec(spec.to_dict())
