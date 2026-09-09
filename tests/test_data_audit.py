"""Native-shaped offline records; no game or model runtime is contacted."""

import copy
from dataclasses import asdict
import hashlib
import json

import pytest

from dfeval import cli, data_audit
from dfeval.experiment import ExperimentConfig
from dfeval.model_observation import MODEL_OBSERVATION_VERSION, model_input_bytes, project_observation, projection_contract
from dfeval.policies import IdlePolicy, json_bytes


def record(root, *, final_stress=2, notebook="", session="source-session"):
    root.mkdir()
    policy = IdlePolicy().public_config()
    config = {**asdict(ExperimentConfig(max_decisions=2, ticks_per_decision=10, max_total_ticks=20,
                                      starting_save_sha256="a" * 64)),
              "policy": policy, "scenario": "drink-maintenance-v1", "model_observation_version": MODEL_OBSERVATION_VERSION}
    def snapshot(tick, stress):
        return {"world_loaded": True, "map_loaded": True, "fortress_mode": True, "paused": True,
                "df_version": "53.16", "dfhack_version": "53.16-r1.1", "absolute_tick": tick,
                "save_directory": "region1", "ui_focus": "dwarfmode", "errors": [],
                "citizens": [{"id": 7, "stress": stress, "dead": False, "needs": []}],
                "known_former_citizens": [], "stocks": {"by_item_type": {"DRINK": {"stack_units": 30}}},
                "brewing": {"available": False, "session": session, "epoch": 1, "events": []}}

    first = snapshot(100, 0)
    events = [{"kind": "run_start", "config": config, "turn": 0}, {"kind": "snapshot", "snapshot": first, "turn": 0}]
    history = []
    previous = first
    for turn in (1, 2):
        seen = project_observation(previous)
        body = model_input_bytes(seen, history)
        decision = {"action": "wait", "reason": "Recorded policy output.", "notebook": notebook}
        raw = json.dumps(decision)
        after = snapshot(100 + turn * 10, turn if turn == 1 else final_stress)
        rows = [{"kind": "policy_input", "observation": seen, "history": copy.deepcopy(history),
                 "model_observation_version": MODEL_OBSERVATION_VERSION, "input_bytes": len(body),
                 "input_sha256": hashlib.sha256(body).hexdigest()},
                {"kind": "policy_response", "error": None, "duration_seconds": 0.1,
                 "exchange": {"response_text": raw, "response_bytes": len(raw.encode()), "response_truncated": False, "usage": None}},
                {"kind": "decision", "decision": decision},
                {"kind": "action_result", "operation": "advance_ticks", "arguments": {"ticks": 10},
                 "result": {"paused": True, "requested_ticks": 10, "elapsed_ticks": 10,
                            "start_absolute_tick": previous["absolute_tick"], "absolute_tick": after["absolute_tick"]}},
                {"kind": "snapshot", "snapshot": after}]
        events.extend({**event, "turn": turn} for event in rows)
        history.append(decision)
        previous = after
    events.append({"kind": "run_end", "turn": 2, "outcome": "budget_exhausted", "detail": "max_decisions", "pause_confirmed": True})
    write_events(root, events)
    manifest = {"schema_version": 1, "protocol": 1, "bridge_sha256": "b" * 64, "state": "finished",
                "interface": "native measurements + wait/brew/finish", "events_file": "events.jsonl",
                "config": config, "policy": policy, "starting_save_sha256": "a" * 64,
                "initial_snapshot_sha256": hashlib.sha256(json_bytes(first)).hexdigest(), "initial_tick": 100,
                "model_observation_version": MODEL_OBSERVATION_VERSION, "model_observation": projection_contract()}
    (root / "manifest.json").write_bytes(json_bytes(manifest))
    return root


def write_events(root, events):
    for index, event in enumerate(events):
        event.update(event=index, wall_seconds=float(index), at="2026-09-08T12:00:00+00:00")
    (root / "events.jsonl").write_bytes(b"".join(json_bytes(event) + b"\n" for event in events))


def events_at(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_bytes().splitlines()]


def test_equal_game_actions_can_have_different_public_words_and_native_consequences(tmp_path):
    left = record(tmp_path / "model-a", notebook="Let the dwarves rest.")
    right = record(tmp_path / "model-b", final_stress=90, notebook="I am waiting for the work to finish.", session="another-session")
    original = {file: file.read_bytes() for root in (left, right) for file in root.iterdir()}
    report = data_audit.audit_runs([left, right])
    pair = report["comparison"]
    assert pair["decision_actions"]["equal"] is True
    assert pair["game_action_requests"]["equal"] is True
    assert pair["full_public_decisions_equal"] is False
    native = pair["native_state"]
    assert native["last_equal_sample_before_difference"]["absolute_tick"] == 110
    first = native["first_observed_divergence"]
    assert first["absolute_tick"] == 120
    assert first["differences"] == [{"path": "/citizens/0/stress", "left_present": True, "right_present": True, "left": 2, "right": 90}]
    assert first["difference_count"] == 1
    assert all(file.read_bytes() == content for file, content in original.items())
    assert report["runs"][0]["source_files"][1]["sha256"] == hashlib.sha256((left / "events.jsonl").read_bytes()).hexdigest()
    assert all(run["record"]["model_input_contract_verified"] is True for run in report["runs"])
    assert "First differing native sample: tick 120" in data_audit.render_audit(report)


def test_duplicate_sample_ticks_are_reported_without_arbitrary_pairing(tmp_path):
    left, right = [record(tmp_path / name) for name in ("a", "b")]
    events = events_at(right)
    duplicate = copy.deepcopy(events[-2])
    duplicate["snapshot"]["citizens"][0]["stress"] = 999
    events.insert(-1, duplicate)
    write_events(right, events)
    native = data_audit.audit_runs([left, right])["comparison"]["native_state"]
    assert native["ambiguous_ticks"] == [120]
    assert native["shared_sample_count"] == 2
    assert native["first_observed_divergence"] is None
    assert native["equal_complete_sampled_trajectory"] is None


def test_field_differences_preserve_missing_null_types_and_json_pointer_escaping():
    report = data_audit._differences({"null": None, "a/b~c": True, "count": 1}, {"a/b~c": 1, "count": 1.0})
    assert report["difference_count"] == 3
    assert next(row for row in report["differences"] if row["path"] == "/null") == {
        "path": "/null", "left_present": True, "right_present": False, "left": None, "right": None}
    assert any(row["path"] == "/a~1b~0c" for row in report["differences"])
    assert data_audit._differences({}, [])["differences"][0]["path"] == ""


def test_difference_output_is_bounded_but_reports_full_count():
    report = data_audit._differences({str(i): "a" * 1000 for i in range(100)}, {})
    assert report["difference_count"] == 100
    assert len(report["differences"]) == 64 and report["differences_truncated"] is True
    assert report["differences"][0]["left"]["value_omitted"] is True


def test_large_shared_pointer_prefix_does_not_amplify_retained_difference_output():
    key = "large/key~\u754c" * 10000
    count = data_audit.MAX_DIFFERENCES + 16
    report = data_audit._differences({key: [0] * count}, {key: [1] * count})
    assert report["difference_count"] == count
    assert report["differences_truncated"] is True
    assert len(report["differences"]) == data_audit.MAX_DIFFERENCES
    assert len(json.dumps(report).encode()) < data_audit.MAX_DIFFERENCES * 800
    for index, row in enumerate(report["differences"]):
        pointer = "/" + key.replace("~", "~0").replace("/", "~1") + "/" + str(index)
        encoded = pointer.encode("utf-8")
        assert row["path"] is None and row["path_omitted"] is True
        assert row["path_bytes"] == len(encoded)
        assert row["path_sha256"] == hashlib.sha256(encoded).hexdigest()
        assert row["path_encoding"] == "utf-8-surrogatepass"
        assert (row["left"], row["right"]) == (0, 1)


def test_pointer_bound_uses_encoded_bytes_and_preserves_exact_short_and_root_paths():
    short = "a" * (data_audit.MAX_POINTER_BYTES - 1)
    assert data_audit._pointer((short,)) == {"path": "/" + short}
    assert data_audit._pointer(()) == {"path": ""}
    assert data_audit._pointer(("a/b~c", "")) == {"path": "/a~1b~0c/"}
    wide = "\u00e9" * (data_audit.MAX_POINTER_BYTES // 2)
    result = data_audit._pointer((wide,))
    assert result["path"] is None
    assert result["path_bytes"] == len(("/" + wide).encode("utf-8"))
    escaped = "\ud800" * data_audit.MAX_POINTER_BYTES
    result = data_audit._pointer((escaped,))
    assert result["path_sha256"] == hashlib.sha256(("/" + escaped).encode("utf-8", errors="surrogatepass")).hexdigest()


def test_text_report_uses_an_explicit_label_for_an_omitted_pointer():
    key = "private-long-field" * 1000
    differences = data_audit._differences({key: 0}, {key: 1})["differences"]
    report = {"runs": [], "comparison": {
        "decision_actions": {"equal": True}, "game_action_requests": {"equal": True},
        "full_public_decisions_equal": True, "comparability_issues": [],
        "native_state": {"first_observed_divergence": {
            "absolute_tick": 120, "left_event": 5, "right_event": 5, "differences": differences},
            "left_only_ticks": [], "right_only_ticks": [], "ambiguous_ticks": []}}}
    rendered = data_audit.render_audit(report)
    assert "[pointer omitted:" in rendered
    assert differences[0]["path_sha256"] in rendered
    assert key not in rendered
    assert "None: 0 -> 1" not in rendered


def test_changed_source_aborts_even_when_each_individual_analysis_used_frozen_bytes(tmp_path, monkeypatch):
    source = record(tmp_path / "source")
    original = data_audit.audit_action_evidence
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        with (source / "events.jsonl").open("ab") as stream:
            stream.write(b"\n")
        return result
    monkeypatch.setattr(data_audit, "audit_action_evidence", changing)
    with pytest.raises(ValueError, match="changed during audit"):
        data_audit.audit_runs([source])


def test_single_run_cli_returns_machine_readable_evidence(tmp_path, capsys):
    source = record(tmp_path / "source")
    assert cli.main(["audit", str(source), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "native_data_audit"
    assert report["comparison"] is None
    assert report["runs"][0]["record"]["complete_record"] is True
    assert report["runs"][0]["action_evidence"]["verified"] is True


def test_invalid_action_is_not_silently_removed_from_equal_sequences():
    events = [{"kind": "decision", "event": 3, "turn": 1, "decision": {"action": "shell"}}]
    result = data_audit._compare_sequence(data_audit._sequence(events), data_audit._sequence(events))
    assert result["equal"] is None and result["left_count"] == 1


def test_malformed_operation_is_reported_without_crashing_pair_inspection(tmp_path):
    left, right = [record(tmp_path / name) for name in ("a", "b")]
    events = events_at(left)
    next(event for event in events if event.get("operation") == "advance_ticks")["operation"] = []
    write_events(left, events)
    result = data_audit.audit_runs([left, right])
    assert result["runs"][0]["action_evidence"]["verified"] is False
    assert result["comparison"]["recorded_setup_matches"] is False


@pytest.mark.parametrize("which", ["duplicate", "empty", "too_many"])
def test_audit_requires_one_or_two_distinct_recordings(tmp_path, which):
    source = record(tmp_path / "source")
    paths = [source, source] if which == "duplicate" else [] if which == "empty" else [source, tmp_path / "b", tmp_path / "c"]
    with pytest.raises(ValueError):
        data_audit.audit_runs(paths)
