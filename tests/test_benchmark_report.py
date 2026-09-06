"""Recorded/fake evidence only: no game, server, model, or network calls."""

from dataclasses import asdict
import copy
import csv
import hashlib
import io
import json

import pytest

from dfeval import benchmark_report as reports
from dfeval.experiment import ExperimentConfig
from dfeval.live import PROTOCOL_VERSION
from dfeval.policies import json_bytes


def snapshot(tick=100, drink=10):
    return {"world_loaded": True, "map_loaded": True, "fortress_mode": True, "paused": True,
            "df_version": "test-df", "dfhack_version": "test-dfhack", "absolute_tick": tick,
            "save_directory": "test-save", "errors": [], "known_former_citizens": [],
            "citizens": [{"id": 1, "dead": False, "stress": 5, "thirst_timer": 10,
                          "needs": [{"type": "DrinkAlcohol", "focus_level": -1, "need_level": 4}]}],
            "stocks": {"by_item_type": {"DRINK": {"stack_units": drink}}}}


def record(root, *, model="test-model", complete=True, final_drink=12):
    root.mkdir()
    policy = {"kind": "chat_completions", "model": model, "is_model": True, "mode": "local",
              "endpoint": "http://localhost:11434/v1/chat/completions", "max_completion_tokens": 512}
    config = {**asdict(ExperimentConfig(starting_save_sha256="a" * 64)), "policy": policy,
              "scenario": "drink-maintenance-v1"}
    first = snapshot()
    manifest = {"schema_version": 1, "protocol": PROTOCOL_VERSION, "bridge_sha256": "b" * 64,
                "interface": "native measurements + wait/brew/finish", "events_file": "events.jsonl",
                "config": config, "policy": policy, "starting_save_sha256": "a" * 64,
                "initial_snapshot_sha256": hashlib.sha256(json_bytes(first)).hexdigest(),
                "initial_tick": 100, "state": "finished" if complete else "running"}
    events = [
        {"kind": "run_start", "config": config, "wall_seconds": 0, "turn": 0},
        {"kind": "snapshot", "snapshot": first, "wall_seconds": 1, "turn": 0},
        {"kind": "policy_input", "observation": first, "history": [], "wall_seconds": 2},
        {"kind": "policy_response", "duration_seconds": 10, "wall_seconds": 12, "error": None,
         "exchange": {"usage": {"completion_tokens": 100, "prompt_tokens": 200}, "response_truncated": False,
                      "telemetry": {"source": "llama.cpp", "completion_tokens": 80, "prompt_tokens": 200,
                                    "generation_seconds": 4, "prompt_seconds": 2, "load_seconds": 1,
                                    "total_seconds": 7}}},
        {"kind": "decision", "decision": {"action": "wait", "reason": "test", "notebook": ""}, "wall_seconds": 12},
        {"kind": "action_result", "operation": "advance_ticks", "arguments": {"ticks": 1200},
         "duration_seconds": 2, "wall_seconds": 14, "result": {
             "paused": True, "elapsed_ticks": 1200, "requested_ticks": 1200,
             "start_absolute_tick": 100, "absolute_tick": 1300}},
        {"kind": "snapshot", "snapshot": snapshot(1300, final_drink), "wall_seconds": 15},
    ]
    if complete:
        events.append({"kind": "run_end", "outcome": "finished", "pause_confirmed": True,
                       "summary": {"observed_tick_span": 99999999}, "wall_seconds": 20})
    write_events(root, events)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "result.json").write_text('{"summary":{"drink_stack_units_change":99999999}}', encoding="utf-8")
    return root


def events_at(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


def write_events(root, events):
    for index, event in enumerate(events):
        event.update(event=index, at="2026-09-05T12:00:00+00:00")
        event.setdefault("turn", 1)
    (root / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_export_recomputes_care_and_distinguishes_all_rates(tmp_path):
    source = record(tmp_path / "run")
    output = tmp_path / "report"
    report = reports.export_report([source], output)
    run = report["runs"][0]
    assert run["summary"]["drink_stack_units_change"] == 2
    assert run["summary"]["brewing"]["confirmed_new_drink_stack_units"] is None
    assert run["decisions"]["accepted_decisions"] == 1
    assert run["decisions"]["invalid_response_count"] == 0
    performance = run["performance"]
    assert performance["native_decoding_tokens_per_second"] == 20
    assert performance["end_to_end_completion_tokens_per_second"] == 10
    assert performance["game_ticks_per_total_wall_second"] == 60
    assert performance["advance"]["ticks_per_second"] == 600
    assert performance["provider_telemetry"]["load_seconds"]["total"] == 1
    assert run["source_files"][1]["sha256"] == hashlib.sha256((source / "events.jsonl").read_bytes()).hexdigest()
    assert set(path.name for path in output.iterdir()) == {"benchmark.json", "runs.csv", "README.md"}
    assert json.loads((output / "benchmark.json").read_text()) == report
    markdown = (output / "README.md").read_text()
    assert "99999999" not in markdown
    assert "Native decoding tokens / second" in markdown
    assert "Advance-operation ticks / second" in markdown
    assert "DrinkAlcohol" in markdown
    assert "unknown" in markdown
    rows = list(csv.DictReader(io.StringIO((output / "runs.csv").read_text())))
    assert rows[0]["native_decoding_tokens_per_second"] == "20.0"
    assert rows[0]["confirmed_new_drink_stack_units"] == ""


def test_aggregates_are_ratios_of_totals_not_means_of_rates(tmp_path):
    source = record(tmp_path / "run")
    events = events_at(source)
    second = copy.deepcopy(events[2:5])
    for event in second:
        event["turn"] = 2
        event["wall_seconds"] += 100
    second[1]["duration_seconds"] = 20
    second[1]["exchange"]["usage"]["completion_tokens"] = 20
    second[1]["exchange"]["telemetry"].update(completion_tokens=20, generation_seconds=1)
    events[-1]["turn"] = 2
    events[-1]["wall_seconds"] = 150
    events[-1:-1] = second
    write_events(source, events)
    performance = reports.build_report([source])["runs"][0]["performance"]
    assert performance["end_to_end_completion_tokens_per_second"] == 4  # 120/30, not (10+1)/2
    assert performance["native_decoding_tokens_per_second"] == 20  # 100/5
    assert performance["completion_tokens"]["total"] == 120


@pytest.mark.parametrize("replacement", [None, {}, {"generation_seconds": -1, "completion_tokens": True}])
def test_unavailable_native_telemetry_is_unknown_without_usage_fallback(tmp_path, replacement):
    source = record(tmp_path / "run")
    events = events_at(source)
    events[3]["exchange"]["telemetry"] = replacement
    write_events(source, events)
    performance = reports.build_report([source])["runs"][0]["performance"]
    assert performance["native_decoding_tokens_per_second"] is None
    assert performance["provider_telemetry"]["generation_seconds"]["total"] is None
    assert performance["end_to_end_completion_tokens_per_second"] == 10


def test_timeout_truncation_unknown_usage_never_become_zero_token_measurements(tmp_path):
    source = record(tmp_path / "run")
    events = events_at(source)
    events[3]["exchange"] = {"usage": None, "response_truncated": True}
    events[3]["error"] = {"type": "PolicyError", "message": "timeout"}
    events = events[:4] + [events[-1]]
    events[-1]["outcome"] = "error"
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["performance"]["completion_tokens"]["total"] is None
    assert run["performance"]["native_decoding_tokens_per_second"] is None
    assert run["performance"]["end_to_end_completion_tokens_per_second"] is None
    assert run["decisions"]["accepted_decisions"] == 0
    assert run["decisions"]["failed_policy_calls"] == 1
    assert run["decisions"]["invalid_response_count"] == 0


def test_decision_error_and_unclassified_missing_responses_are_distinguished(tmp_path):
    source = record(tmp_path / "run")
    events = events_at(source)
    events[3]["error"] = {"type": "DecisionError", "message": "unknown action"}
    events = events[:4] + [events[-1]]
    events[-1]["outcome"] = "error"
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["decisions"]["invalid_response_count"] == 1
    events.pop(3)
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["decisions"]["invalid_response_count"] is None
    assert run["decisions"]["unclassified_response_count"] == 1
    assert run["performance"]["policy_wall_seconds"]["total"] is None


def test_partial_calls_keep_known_subset_but_suppress_aggregate(tmp_path):
    source = record(tmp_path / "run")
    events = events_at(source)
    events.insert(-1, {"kind": "policy_input", "turn": 2, "wall_seconds": 16})
    events[-1]["turn"] = 2
    write_events(source, events)
    performance = reports.build_report([source])["runs"][0]["performance"]
    assert performance["completion_tokens"] == {"total": None, "known_total": 100, "measured": 1, "unknown": 1}
    assert performance["policy_wall_seconds"]["known_total"] == 10
    assert performance["end_to_end_completion_tokens_per_second"] is None
    assert performance["native_decoding_tokens_per_second"] is None


def test_orphan_or_out_of_order_decisions_do_not_inflate_acceptance(tmp_path):
    source = record(tmp_path / "source")
    events = events_at(source)
    events[2], events[4] = events[4], events[2]
    # Keep the event envelope coherent but the semantic call order wrong.
    events[2]["wall_seconds"] = 2
    events[4]["wall_seconds"] = 12
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["decisions"]["accepted_decisions"] == 0
    assert run["decisions"]["unclassified_response_count"] == 1
    assert any("not counted as accepted" in warning for warning in run["warnings"])
    events = [event for event in events if event["kind"] not in ("policy_input", "policy_response")]
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["decisions"]["accepted_decisions"] == 0
    assert run["decisions"]["policy_calls"] == 0


def test_historical_baseline_decision_validation_error_is_classified(tmp_path):
    source = record(tmp_path / "source")
    events = events_at(source)
    events = events[:4] + [{"kind": "error", "type": "DecisionError", "wall_seconds": 13}] + [events[-1]]
    events[-1]["outcome"] = "error"
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["decisions"]["invalid_response_count"] == 1


def test_host_metadata_preserved_without_arbitrary_private_fields(tmp_path):
    source = record(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["host"] = {"system": "Windows", "machine": "AMD64", "processor": "test",
                        "logical_cpus": 12, "python": "3.12", "hostname": "private-host"}
    (source / "manifest.json").write_text(json.dumps(manifest))
    hardware = reports.build_report([source])["runs"][0]["hardware"]
    assert hardware["logical_cpu_count"] == 12
    assert hardware["system"] == "Windows"
    assert "hostname" not in hardware


def test_incomplete_prefix_discloses_wall_time_and_retains_care(tmp_path):
    source = record(tmp_path / "run", complete=False)
    with (source / "events.jsonl").open("a") as stream:
        stream.write('{"kind":')
    run = reports.build_report([source])["runs"][0]
    assert run["complete_record"] is False
    assert run["outcome"] is None
    assert run["performance"]["total_wall_seconds"] is None
    assert run["performance"]["recorded_wall_span_seconds"] == 15
    assert run["performance"]["game_ticks_per_total_wall_second"] is None
    assert run["summary"]["drink_stack_units_change"] == 2
    assert any("Incomplete final log" in warning for warning in run["warnings"])


@pytest.mark.parametrize("mutation", ["save", "backwards", "boundary", "missing_duration", "zero_duration"])
def test_simulation_rates_require_valid_boundaries_and_measured_duration(tmp_path, mutation):
    source = record(tmp_path / "run")
    events = events_at(source)
    if mutation == "save":
        events[6]["snapshot"]["save_directory"] = "different"
    elif mutation == "backwards":
        events[6]["snapshot"]["absolute_tick"] = 50
    elif mutation == "boundary":
        events[5]["result"]["absolute_tick"] += 1
    elif mutation == "missing_duration":
        events[5].pop("duration_seconds")
    else:
        events[5]["duration_seconds"] = 0
    write_events(source, events)
    run = reports.build_report([source])["runs"][0]
    assert run["performance"]["advance"]["ticks_per_second"] is None


def test_multiple_runs_reuse_comparability_checks_without_ranking(tmp_path):
    first, second = record(tmp_path / "first"), record(tmp_path / "second", model="other", final_drink=20)
    report = reports.build_report([first, second])
    assert report["recorded_setup_matches"] is True
    assert [run["name"] for run in report["runs"]] == ["first", "second"]
    assert [run["summary"]["drink_stack_units_change"] for run in report["runs"]] == [2, 10]
    manifest = json.loads((second / "manifest.json").read_text())
    manifest["config"]["max_decisions"] += 1
    (second / "manifest.json").write_text(json.dumps(manifest))
    report = reports.build_report([first, second])
    assert report["recorded_setup_matches"] is False
    assert report["comparability_issues"]


@pytest.mark.parametrize("label", ["=1+1", "+SUM(A1)", "-1+1", "@SUM(A1)", "\tmalicious", "\rformula", "  =1"])
def test_csv_formula_guard_for_model_and_run_labels(tmp_path, label):
    source = record(tmp_path / "safe", model=label)
    report = reports.build_report([source])
    report["runs"][0]["name"] = label
    row = next(csv.DictReader(io.StringIO(reports.render_csv(report))))
    assert row["run"] == "'" + label
    assert row["model"] == "'" + label
    assert row["policy"] == "'" + label


def test_markdown_does_not_render_recorded_html_or_links(tmp_path):
    source = record(tmp_path / "run", model='<img src=x>\n[unsafe](https://example.invalid)')
    text = reports.render_report(reports.build_report([source]))
    assert "<img" not in text
    assert "[unsafe](" not in text
    assert "&lt;img" in text


def test_output_new_empty_only_and_source_evidence_unchanged(tmp_path):
    source = record(tmp_path / "source")
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    out = tmp_path / "out"
    out.mkdir()
    reports.export_report([source], out)
    with pytest.raises(ValueError, match="new or empty"):
        reports.export_report([source], out)
    with pytest.raises(ValueError, match="separate"):
        reports.export_report([source], source)
    assert original == {path.name: path.read_bytes() for path in source.iterdir()}


def test_changed_sources_are_rejected_before_output_is_created(tmp_path, monkeypatch):
    source = record(tmp_path / "source")
    reader = reports.read_run
    def changing(path):
        result = reader(path)
        with (source / "events.jsonl").open("a") as stream:
            stream.write("\n")
        return result
    monkeypatch.setattr(reports, "read_run", changing)
    out = tmp_path / "report"
    with pytest.raises(ValueError, match="changed"):
        reports.export_report([source], out)
    assert not out.exists()


def test_malformed_duplicate_keys_mock_and_missing_native_evidence_are_rejected(tmp_path):
    source = record(tmp_path / "source")
    log = source / "events.jsonl"
    original = log.read_bytes()
    log.write_bytes(original + b'{"kind":"error","kind":"error"}\n')
    with pytest.raises(ValueError, match="Malformed complete event"):
        reports.build_report([source])
    log.write_bytes(b'{"event":0,"kind":"run_start","backend":"mock"}\n')
    with pytest.raises(ValueError, match="mock"):
        reports.build_report([source])
    log.write_bytes(b'{"event":0,"kind":"run_start"}\n')
    with pytest.raises(ValueError, match="no native"):
        reports.build_report([source])


def test_bounded_inputs_and_evidence_size(tmp_path, monkeypatch):
    source = record(tmp_path / "source")
    for paths in ([], [source, source], str(source)):
        with pytest.raises(ValueError):
            reports.build_report(paths)
    with pytest.raises(ValueError, match="at most 100"):
        reports.build_report(tmp_path / str(i) for i in range(101))
    monkeypatch.setattr(reports, "MAX_LOG_BYTES", 32)
    with pytest.raises(ValueError, match="exceeds inspection"):
        reports.build_report([source])


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, True, "1"])
def test_invalid_and_nonfinite_measurements_do_not_create_rates(value):
    assert reports._number(value) is None
    assert reports._ratio(value, 1) is None
    assert reports._ratio(1, value) is None
