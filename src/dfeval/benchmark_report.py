"""Inspectable exports of recorded native care outcomes and execution timings.

No game, model, or stored result summary is executed or trusted here. Rates are
ratios of corresponding totals, never averages of per-call rates. Missing
measurements remain null; known subsets are explicitly labeled. The report
does not rank dwarf care by computer speed or combine outcomes into a score.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
from pathlib import Path
from typing import Any, Iterable

from .comparison import MAX_LOG_BYTES, _evidence_json, _read, compare_runs, read_run
from .policies import DecisionError, validate_decision


SCHEMA_VERSION = 1
MANIFEST_LIMIT = 4 * 1024 * 1024
NOTES = [
    "Care outcomes are recomputed from native snapshot events; result.json and stored summary scores are ignored.",
    "No care ranking or composite wellbeing score is produced. Hardware speed does not establish better care.",
    "Null in JSON, blank in CSV, and unknown in this report mean unmeasured or invalid, never an imputed zero.",
    "Native decoding tokens/second uses provider-reported generation tokens and generation time. It excludes prompt processing and host overhead.",
    "End-to-end completion tokens/second uses reported completion usage divided by host policy-call wall time; it includes prompt processing, network, queueing, and decoding.",
    "Game ticks/total wall second includes policy waits and setup. Advance-operation ticks/second excludes policy waits but includes bridge polling and pause overhead; it is not a renderer FPS measurement.",
    "Aggregate timing and usage totals are unknown if any contributing call is unmeasured. Known subsets and per-call values remain available in benchmark.json.",
    "Timings and token counts are reported by the host/provider, not independent hardware measurements. Source hashes identify inspected files; they do not authenticate their author or prove an untouched game.",
    "Incomplete records describe only the retained prefix. Matching recorded setup does not establish deterministic simulation, causality, or statistical significance.",
    "Installation fingerprints cover only the local files and limitations listed in each manifest. External settings, active plugin state, and server-side model configuration are not proven equal; older runs without this inventory leave that scope unverified.",
]


def _number(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    numerator, denominator = _number(numerator), _number(denominator)
    if numerator is None or denominator is None or denominator == 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def _total(values: Iterable[Any]) -> dict[str, Any]:
    values = list(values)
    known = [value for value in values if _number(value) is not None]
    amount = sum(known)
    if _number(amount) is None:
        amount = None
    return {"total": amount if len(known) == len(values) else None,
            "known_total": amount if known else None,
            "measured": len(known), "unknown": len(values) - len(known)}


def _source(path: Path, limit: int) -> tuple[bytes, dict[str, Any]]:
    data = _read(path, limit)
    return data, {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _events(raw: bytes) -> list[dict[str, Any]]:
    """Mirror the comparison reader's handling of a torn final JSONL record."""
    lines = raw.splitlines(keepends=True)
    events = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = _evidence_json(line.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("Event must be an object")
        except (ValueError, UnicodeError, RecursionError):
            if index == len(lines) - 1 and not line.endswith((b"\n", b"\r")):
                continue
            raise ValueError("Malformed complete event in recorded evidence") from None
        events.append(value)
    return events


def _public_error(event: dict[str, Any]) -> str | None:
    error = event.get("error")
    return _text(error.get("type")) if isinstance(error, dict) else None


def _calls(events: list[dict[str, Any]], *, is_model: bool, warn) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Associate runner calls by turn, without treating a missing action as invalid."""
    groups: dict[int, dict[str, list[dict[str, Any]]]] = {}
    invalid_decision_events = 0
    errors_by_turn = {event.get("turn"): _text(event.get("type")) for event in events
                      if event.get("kind") == "error" and _integer(event.get("turn")) is not None}
    for event in events:
        kind = event.get("kind")
        if kind not in ("policy_input", "policy_response", "decision"):
            continue
        if kind == "decision":
            try:
                validate_decision(event.get("decision"))
            except DecisionError:
                invalid_decision_events += 1
        turn = _integer(event.get("turn"))
        if turn is None:
            warn("Policy records with invalid turn numbers cannot be associated with calls.")
            continue
        groups.setdefault(turn, {}).setdefault(kind, []).append(event)
    calls = []
    for turn, group in groups.items():
        inputs, responses, decisions = (group.get(key, []) for key in ("policy_input", "policy_response", "decision"))
        if len(inputs) != 1 or len(responses) > 1 or len(decisions) > 1:
            warn("Policy input/response/decision records cannot be paired uniquely in one or more turns.")
        # Retain each input attempt. Ambiguous/orphan responses are not used for rates.
        for index in range(max(1, len(inputs))):
            response = responses[0] if len(inputs) == len(responses) == 1 else {}
            exchange = response.get("exchange") if isinstance(response.get("exchange"), dict) else {}
            usage = exchange.get("usage") if isinstance(exchange.get("usage"), dict) else {}
            telemetry = exchange.get("telemetry") if isinstance(exchange.get("telemetry"), dict) else {}
            duration = _number(response.get("duration_seconds"))
            if duration is not None and _number(response.get("wall_seconds")) is not None and duration > response["wall_seconds"]:
                warn("A policy duration exceeds its event's elapsed wall time; that duration is suppressed.")
                duration = None
            error_type = _public_error(response) or errors_by_turn.get(turn)
            accepted = False
            ordered = (len(inputs) == len(responses) == len(decisions) == 1 and
                       all(_integer(item.get("event")) is not None for item in (inputs[0], responses[0], decisions[0])) and
                       inputs[0]["event"] < responses[0]["event"] < decisions[0]["event"])
            if ordered:
                try:
                    validate_decision(decisions[0].get("decision"))
                    # Later action failures do not revoke an accepted decision.
                    accepted = _public_error(response) is None
                except DecisionError:
                    pass
            if decisions and not accepted:
                warn("A decision record lacks a unique, ordered successful policy exchange; it is not counted as accepted.")
            if accepted:
                state = "accepted"
            elif error_type == "DecisionError":
                state = "invalid"
            elif error_type:
                state = "failed"
            elif response and response.get("decision_valid") is False:
                state = "invalid"
            else:
                state = "unclassified"
            completion = _integer(usage.get("completion_tokens")) if is_model else None
            prompt = _integer(usage.get("prompt_tokens")) if is_model else None
            native_completion = _integer(telemetry.get("completion_tokens")) if is_model else None
            native_prompt = _integer(telemetry.get("prompt_tokens")) if is_model else None
            timing = {key: _number(telemetry.get(key)) if is_model else None for key in
                      ("generation_seconds", "prompt_seconds", "load_seconds", "total_seconds")}
            calls.append({
                "turn": turn, "input_event": inputs[index].get("event") if index < len(inputs) else None,
                "response_event": response.get("event"), "state": state,
                "error_type": error_type, "response_truncated": exchange.get("response_truncated")
                    if type(exchange.get("response_truncated")) is bool else None,
                "duration_seconds": duration, "completion_tokens": completion, "prompt_tokens": prompt,
                "telemetry": {"source": _text(telemetry.get("source")), **timing,
                              "completion_tokens": native_completion, "prompt_tokens": native_prompt},
                "native_decoding_tokens_per_second": _ratio(native_completion, timing["generation_seconds"]),
                "end_to_end_completion_tokens_per_second": _ratio(completion, duration),
            })
    if invalid_decision_events:
        warn("Some recorded decision events fail the current strict action schema and are excluded from accepted decisions.")
    confirmed_invalid = sum(call["state"] == "invalid" for call in calls)
    unclassified = sum(call["state"] == "unclassified" for call in calls)
    return calls, {"policy_calls": sum(len(group.get("policy_input", [])) for group in groups.values()),
                   "accepted_decisions": sum(call["state"] == "accepted" for call in calls),
                   "invalid_response_count": confirmed_invalid if not unclassified else None,
                   "confirmed_invalid_response_count": confirmed_invalid,
                   "unclassified_response_count": unclassified,
                   "failed_policy_calls": sum(call["state"] == "failed" for call in calls),
                   "invalid_decision_events": invalid_decision_events}


def _advance(events: list[dict[str, Any]], *, continuity: bool, warn) -> dict[str, Any]:
    records = []
    # Index adjacent snapshots once, keeping a 64 MiB transcript linear to inspect.
    following = {}
    next_snapshot = None
    for index in range(len(events) - 1, -1, -1):
        following[index] = next_snapshot
        if events[index].get("kind") == "snapshot":
            next_snapshot = events[index].get("snapshot")
    previous_snapshot = None
    for index, event in enumerate(events):
        if event.get("kind") == "snapshot":
            previous_snapshot = event.get("snapshot")
        if event.get("kind") != "action_result" or event.get("operation") != "advance_ticks":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        duration = _number(event.get("duration_seconds"))
        elapsed = _integer(result.get("elapsed_ticks"))
        start, end, requested = (_integer(result.get(key)) for key in
                                 ("start_absolute_tick", "absolute_tick", "requested_ticks"))
        before, after = previous_snapshot, following[index]
        valid = (continuity and not event.get("error") and result.get("paused") is True
                 and all(value is not None for value in (elapsed, start, end, requested))
                 and end - start == elapsed == requested == _integer(arguments.get("ticks"))
                 and isinstance(before, dict) and isinstance(after, dict)
                 and before.get("absolute_tick") == start and after.get("absolute_tick") == end)
        if duration is not None and _number(event.get("wall_seconds")) is not None and duration > event["wall_seconds"]:
            duration = None
        if not valid:
            warn("An advance lacks matching native tick boundaries; its ticks are excluded from simulation throughput.")
        records.append({"event": event.get("event"), "duration_seconds": duration,
                        "validated_elapsed_ticks": elapsed if valid else None})
    ticks = _total(record["validated_elapsed_ticks"] for record in records)
    wall = _total(record["duration_seconds"] for record in records)
    return {"calls": records, "ticks": ticks, "wall_seconds": wall,
            "ticks_per_second": _ratio(ticks["total"], wall["total"])}


def _performance(run: dict[str, Any], events: list[dict[str, Any]], warn) -> tuple[dict[str, Any], dict[str, Any]]:
    calls, decisions = _calls(events, is_model=run["policy_config"].get("is_model") is True, warn=warn)
    endings = [event for event in events if event.get("kind") == "run_end"]
    wall = _number(endings[0].get("wall_seconds")) if len(endings) == 1 and run["complete_record"] else None
    observed_wall = max((event["wall_seconds"] for event in events if _number(event.get("wall_seconds")) is not None), default=None)
    host = _total(call["duration_seconds"] for call in calls)
    completion = _total(call["completion_tokens"] for call in calls)
    prompt = _total(call["prompt_tokens"] for call in calls)
    native = {key: _total(call["telemetry"][key] for call in calls) for key in
              ("generation_seconds", "prompt_seconds", "load_seconds", "total_seconds", "completion_tokens", "prompt_tokens")}
    advance = _advance(events, continuity=run["world_time_continuity_verified"], warn=warn)
    if host["total"] is not None and observed_wall is not None and host["total"] > observed_wall:
        warn("Summed policy-call durations exceed the recorded wall time; aggregate policy time and end-to-end rate are suppressed.")
        host["total"] = None
    span = run["summary"].get("observed_tick_span")
    return {"total_wall_seconds": wall, "recorded_wall_span_seconds": observed_wall,
            "observed_game_ticks": span,
            "game_ticks_per_total_wall_second": _ratio(span, wall),
            "policy_wall_seconds": host, "completion_tokens": completion, "prompt_tokens": prompt,
            "provider_telemetry": native,
            "native_decoding_tokens_per_second": _ratio(native["completion_tokens"]["total"], native["generation_seconds"]["total"]),
            "end_to_end_completion_tokens_per_second": _ratio(completion["total"], host["total"]),
            "advance": advance, "policy_calls": calls}, decisions


def build_report(run_dirs: Iterable[str | Path]) -> dict[str, Any]:
    """Read 1..100 distinct native recordings without mutating them.

    Evidence is bounded by the comparison reader's limits. Before/after hashes
    reject concurrently changing recordings instead of pairing stale validation
    with a new event stream. Stop a run before exporting its report.
    """
    if isinstance(run_dirs, (str, bytes, Path)):
        raise ValueError("Pass a sequence of 1..100 run directories")
    paths = []
    for path in run_dirs:
        paths.append(Path(path).expanduser().resolve())
        if len(paths) > 100:
            raise ValueError("Export at most 100 recorded runs")
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("Choose 1..100 distinct recorded run directories")
    hashes = []
    for path in paths:
        hashes.append([_source(path / name, limit)[1] for name, limit in
                       (("manifest.json", MANIFEST_LIMIT), ("events.jsonl", MAX_LOG_BYTES))])
    comparison = compare_runs(paths) if len(paths) > 1 else None
    validated = comparison["runs"] if comparison else [read_run(paths[0])]
    runs = []
    for index, (path, run) in enumerate(zip(paths, validated)):
        manifest_raw, manifest_hash = _source(path / "manifest.json", MANIFEST_LIMIT)
        event_raw, event_hash = _source(path / "events.jsonl", MAX_LOG_BYTES)
        if hashes[index] != [manifest_hash, event_hash]:
            raise ValueError(f"Recording changed while it was inspected: {path.name}. Stop the run and retry.")
        manifest = _evidence_json(manifest_raw.decode("utf-8-sig"))
        events = _events(event_raw)
        warnings = list(run["warnings"])
        def warn(message: str) -> None:
            if message not in warnings:
                warnings.append(message)
        performance, decisions = _performance(run, events, warn)
        policy = run["policy_config"]
        hardware = manifest.get("host") if isinstance(manifest.get("host"), dict) else {}
        if not hardware and isinstance(manifest.get("hardware"), dict):
            hardware = manifest["hardware"]
        ends = [event for event in events if event.get("kind") == "run_end"]
        speed_restored = ends[-1].get("speed_restored") if ends else None
        runs.append({**run, "warnings": warnings, "report_index": index + 1,
                     "model": _text(policy.get("model")), "provider": _text(policy.get("kind")),
                     "connection_mode": _text(policy.get("mode")), "endpoint": _text(policy.get("endpoint")),
                     "is_model": policy.get("is_model") if type(policy.get("is_model")) is bool else None,
                     "simulation_fps_requested": _number(manifest.get("config", {}).get("simulation_fps")),
                     "speed_restored": speed_restored if type(speed_restored) is bool else None,
                     "hardware": {**{key: _text(hardware.get(key)) for key in
                                     ("system", "machine", "processor", "python")},
                                  "logical_cpu_count": _integer(hardware.get("logical_cpus", hardware.get("logical_cpu_count")))},
                     "source_files": [manifest_hash, event_hash], "decisions": decisions,
                     "performance": performance})
    return {"schema_version": SCHEMA_VERSION, "kind": "native_care_benchmark_report", "runs": runs,
            "recorded_setup_matches": comparison["recorded_setup_matches"] if comparison else None,
            "comparability_issues": comparison["comparability_issues"] if comparison else [],
            "notes": list(NOTES)}


def _show(value: Any) -> str:
    if value is None:
        return "unknown"
    if type(value) is float:
        return f"{value:.6g}" if math.isfinite(value) else "unknown"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    # Do not allow recorded model/run labels to introduce Markdown or raw HTML.
    text = html.escape(str(value), quote=False).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    for char in ("\\", "`", "*", "_", "[", "]", "|", "#", "!"):
        text = text.replace(char, "\\" + char)
    return text


def render_report(report: dict[str, Any]) -> str:
    """Render human-readable outcomes with units, missingness, and evidence limits."""
    lines = ["# Recorded Dwarf Fortress care benchmark", "",
             "Care outcomes and execution performance are separate measurements. No model ranking is assigned.", "",
             "Files: `benchmark.json` contains full measurements and per-call telemetry; `runs.csv` contains one row per run.", "",
             "Comparison checks: " + ("not applicable to one run" if report["recorded_setup_matches"] is None else
                 "recorded setup matches" if report["recorded_setup_matches"] else
                 "qualified by recorded differences, failures, or missing evidence") + ".", ""]
    for run in report["runs"]:
        summary, performance, decisions = run["summary"], run["performance"], run["decisions"]
        first, last, brew = summary["initial"], summary["final"], summary["brewing"]
        lines.extend([f"## Run {run['report_index']}: {_show(run['name'])}", "",
                      f"Policy/model: {_show(run['policy'])}; provider adapter: {_show(run['provider'])}; connection: {_show(run['connection_mode'])}.",
                      f"Outcome: {_show(run['outcome'])}; final pause confirmed: {_show(run['pause_confirmed'])}; complete recording: {_show(run['complete_record'])}.", "",
                      "Reference starting-state check: " + ("not requested" if run.get("initial_expectation") is None else
                          "matched" if (run.get("initial_state_check") or {}).get("matched") is True else
                          "mismatched or unverified") + ".", "",
                      f"Recorded installation configuration: {_show(run.get('game_environment_sha256'))} (declared file scope only).", "",
                      "| Native care measurement | Initial | Final |", "| --- | ---: | ---: |",
                      f"| Living population | {_show(first['population'])} | {_show(last['population'])} |",
                      f"| Drink stack units | {_show(first['drink']['stack_units'])} | {_show(last['drink']['stack_units'])} |"])
        for key, title in (("stress", "Stress"), ("thirst_timer", "Thirst timer"), ("hunger_timer", "Hunger timer")):
            a, b = (sample["citizen_measurements"].get(key) for sample in (first, last))
            lines.append(f"| {title} median (raw) | {_show(a.get('median') if a else None)} | {_show(b.get('median') if b else None)} |")
            lines.append(f"| {title} unknown citizens | {_show(a.get('unknown') if a else None)} | {_show(b.get('unknown') if b else None)} |")
        lines.extend(["", f"Observed confirmed deaths: {_show(summary['observed_confirmed_deaths'])}.",
                      f"Jobs with confirmed new drink products: {_show(brew['jobs_with_confirmed_drink_products'])}; new drink stack units: {_show(brew['confirmed_new_drink_stack_units'])}; production evidence complete: {_show(brew['evidence_complete'])}.",
                      f"Accepted decisions: {_show(decisions['accepted_decisions'])}; invalid responses: {_show(decisions['invalid_response_count'])} (confirmed: {_show(decisions['confirmed_invalid_response_count'])}; unclassified: {_show(decisions['unclassified_response_count'])}); failed calls: {_show(decisions['failed_policy_calls'])}.", "",
                      "| Execution measurement | Value |", "| --- | ---: |"])
        measurements = [
            ("Observed game ticks", performance["observed_game_ticks"]),
            ("Total run wall seconds", performance["total_wall_seconds"]),
            ("Game ticks / total wall second", performance["game_ticks_per_total_wall_second"]),
            ("Advance-operation wall seconds", performance["advance"]["wall_seconds"]["total"]),
            ("Advance-operation ticks / second", performance["advance"]["ticks_per_second"]),
            ("Policy-call wall seconds", performance["policy_wall_seconds"]["total"]),
            ("Reported completion tokens", performance["completion_tokens"]["total"]),
            ("Reported prompt tokens", performance["prompt_tokens"]["total"]),
            ("Native decoding tokens / second", performance["native_decoding_tokens_per_second"]),
            ("End-to-end completion tokens / policy wall second", performance["end_to_end_completion_tokens_per_second"]),
        ]
        measurements.extend(("Provider " + key.replace("_", " "), performance["provider_telemetry"][key]["total"])
                            for key in ("generation_seconds", "prompt_seconds", "load_seconds", "total_seconds"))
        lines.extend(f"| {name} | {_show(value)} |" for name, value in measurements)
        lines.extend(["", "Native needs by type (raw focus/need distributions):", "",
                      "| Need | Initial focus median | Final focus median | Initial need median | Final need median |", "| --- | ---: | ---: | ---: | ---: |"])
        initial_needs, final_needs = first.get("needs"), last.get("needs")
        kinds = sorted(set(initial_needs or {}) | set(final_needs or {}))
        for kind in kinds:
            a, b = (needs.get(kind, {}) if isinstance(needs, dict) else {} for needs in (initial_needs, final_needs))
            values = [sample.get(key, {}).get("median") for key in ("focus_level", "need_level") for sample in (a, b)]
            lines.append("| " + " | ".join([_show(kind), *(_show(value) for value in values)]) + " |")
        if not kinds:
            lines.append("| No recorded need types; see missingness below | unknown | unknown | unknown | unknown |")
        lines.extend(["", f"Citizens with unknown needs: {_show(first['citizens_with_unknown_needs'])} initially, {_show(last['citizens_with_unknown_needs'])} finally.", "", "Source evidence:", ""])
        lines.extend(f"- `{source['file']}`: SHA-256 `{source['sha256']}`, {source['bytes']} bytes." for source in run["source_files"])
        if run["warnings"]:
            lines.extend(["", "Evidence limitations:", ""])
            lines.extend("- " + _show(warning) for warning in run["warnings"])
        lines.append("")
    if report["comparability_issues"]:
        lines.extend(["## Comparison limitations", ""])
        lines.extend("- " + _show(issue) for issue in report["comparability_issues"])
        lines.append("")
    lines.extend(["## Measurement definitions", ""])
    lines.extend("- " + note for note in report["notes"])
    return "\n".join(lines) + "\n"


def _csv_value(value: Any) -> Any:
    if value is None or (type(value) is float and not math.isfinite(value)):
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if isinstance(value, str) and (value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or
                                   value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def render_csv(report: dict[str, Any]) -> str:
    """One row per run; guard spreadsheet formula injection in all text cells."""
    rows = []
    for run in report["runs"]:
        summary, performance = run["summary"], run["performance"]
        first, last, brew = summary["initial"], summary["final"], summary["brewing"]
        row = {"run": run["name"], "model": run["model"], "policy": run["policy"],
               "provider": run["provider"], "connection_mode": run["connection_mode"], "is_model": run["is_model"],
               "outcome": run["outcome"], "pause_confirmed": run["pause_confirmed"], "complete_record": run["complete_record"],
               "reference_start_matched": (run.get("initial_state_check") or {}).get("matched"),
               "starting_save_sha256": run["declared_starting_save_sha256"],
               "initial_observation_sha256": run["initial_observation_sha256"],
               "game_environment_sha256": run.get("game_environment_sha256"),
               **run["decisions"], "initial_population": first["population"], "final_population": last["population"],
               "observed_confirmed_deaths": summary["observed_confirmed_deaths"],
               "initial_drink_stack_units": first["drink"]["stack_units"], "final_drink_stack_units": last["drink"]["stack_units"],
               "jobs_with_confirmed_drink_products": brew["jobs_with_confirmed_drink_products"],
               "confirmed_new_drink_stack_units": brew["confirmed_new_drink_stack_units"], "production_evidence_complete": brew["evidence_complete"],
               "initial_needs_json": first["needs"], "final_needs_json": last["needs"],
               "initial_unknown_needs_citizens": first["citizens_with_unknown_needs"], "final_unknown_needs_citizens": last["citizens_with_unknown_needs"]}
        for label, sample in (("initial", first), ("final", last)):
            for measure in ("stress", "thirst_timer", "hunger_timer"):
                distribution = sample["citizen_measurements"].get(measure) or {}
                for statistic in ("median", "max", "unknown"):
                    row[f"{label}_{measure}_{statistic}"] = distribution.get(statistic)
        for key in ("observed_game_ticks", "total_wall_seconds", "game_ticks_per_total_wall_second",
                    "native_decoding_tokens_per_second", "end_to_end_completion_tokens_per_second"):
            row[key] = performance[key]
        row.update(advance_wall_seconds=performance["advance"]["wall_seconds"]["total"],
                   advance_ticks_per_second=performance["advance"]["ticks_per_second"],
                   policy_wall_seconds=performance["policy_wall_seconds"]["total"],
                   reported_completion_tokens=performance["completion_tokens"]["total"],
                   reported_prompt_tokens=performance["prompt_tokens"]["total"])
        for key in ("generation_seconds", "prompt_seconds", "load_seconds", "total_seconds"):
            row["provider_" + key] = performance["provider_telemetry"][key]["total"]
        row.update(simulation_fps_requested=run["simulation_fps_requested"], speed_restored=run["speed_restored"],
                   warnings_json=run["warnings"], manifest_sha256=run["source_files"][0]["sha256"],
                   events_sha256=run["source_files"][1]["sha256"])
        rows.append(row)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else [])
    writer.writeheader()
    writer.writerows({key: _csv_value(value) for key, value in row.items()} for row in rows)
    return output.getvalue()


def export_report(run_dirs: Iterable[str | Path], output_dir: str | Path) -> dict[str, Any]:
    """Export benchmark.json, runs.csv, and README.md to a new/empty directory.

    The returned report is the same object serialized in benchmark.json. The
    destination is never populated until all source evidence has been checked.
    Existing files, symlink destinations, and the source directory itself are rejected.
    """
    if isinstance(run_dirs, (str, bytes, Path)):
        raise ValueError("Pass a sequence of 1..100 run directories")
    paths = []
    for path in run_dirs:
        paths.append(Path(path).expanduser().resolve())
        if len(paths) > 100:
            raise ValueError("Export at most 100 recorded runs")
    target = Path(output_dir).expanduser()
    if target.is_symlink():
        raise ValueError("Report output directory must not be a symbolic link")
    target = target.resolve()
    if target in paths:
        raise ValueError("Report output must be separate from source run directories")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Report output directory must be new or empty")
    report = build_report(paths)
    json_report = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    csv_report, markdown_report = render_csv(report), render_report(report)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError("Report output directory changed while evidence was inspected")
    for name, text in (("benchmark.json", json_report), ("runs.csv", csv_report), ("README.md", markdown_report)):
        with (target / name).open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
    return report
