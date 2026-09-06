"""Compare recorded native outcomes without trusting stored summary scores."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from .care import summarize_events
from .live import PROTOCOL_VERSION
from .model_observation import (MODEL_OBSERVATION_VERSION, ModelObservationError,
                                model_input_bytes, project_observation, projection_contract)
from .policies import json_bytes, strict_json

MAX_LOG_BYTES = 64 * 1024 * 1024
COMPARISON_FIELDS = (
    "max_decisions", "ticks_per_decision", "max_total_ticks", "max_policy_calls",
    "max_bridge_calls", "max_output_tokens", "max_output_bytes", "max_wall_seconds",
    "request_timeout", "max_input_bytes", "max_snapshot_bytes", "max_log_bytes", "history_decisions",
)
NATIVE_FIELDS = frozenset({"df_version", "dfhack_version", "world_loaded", "map_loaded", "fortress_mode",
                           "absolute_tick", "citizens", "stocks"})


class _EvidenceDepthError(ValueError):
    pass


def _evidence_json(text: str) -> Any:
    value = strict_json(text)
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise _EvidenceDepthError("JSON nesting exceeds the inspection limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def _digest(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def _version(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _reported_fps(value: Any) -> int | None:
    """Normalize descriptive native caps, including Lua's integral JSON floats.

    This does not normalize the initial-state fingerprint or any guard identity.
    """
    return int(value) if _finite(value) and 1 <= value <= 10000 and value == int(value) else None


def initial_observation_fingerprint(snapshot: dict[str, Any]) -> str:
    """Hash measured initial state, excluding explicitly session-owned metadata.

    Native time, roster, timers, needs, item IDs/flags, workshop/jobs and effective
    simulation/graphics caps remain included, as do unrecognized future fields.
    UI focus/pause, former-citizen observation history, product callback history,
    and FPS override bookkeeping are not saved-world state. This is a comparison
    fingerprint, not the separate exact-byte initial_snapshot_sha256 integrity
    hash, and cannot prove that unobserved game state or RNG state is identical.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Initial native observation must be an object")
    comparable = {key: value for key, value in snapshot.items() if key not in ("ui_focus", "paused")}
    # Preserve the shape/hash of historical observations with an empty history.
    if isinstance(comparable.get("known_former_citizens"), list):
        comparable["known_former_citizens"] = []
    if isinstance(comparable.get("brewing"), dict):
        historical = {"session", "epoch", "events", "event_count", "dropped_events", "error_count",
                      "last_error", "queued_jobs", "notes"}
        comparable["brewing"] = {key: value for key, value in comparable["brewing"].items() if key not in historical}
    if isinstance(comparable.get("simulation_fps"), dict):
        bookkeeping = {"original", "requested", "original_graphics_cap", "override_active",
                       "restore_error", "capture_error", "restore_reason"}
        comparable["simulation_fps"] = {key: value for key, value in comparable["simulation_fps"].items() if key not in bookkeeping}
    try:
        encoded = json.dumps(comparable, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("Initial native observation must contain finite JSON data") from None
    return hashlib.sha256(encoded).hexdigest()


def validate_initial_expectation(value: Any) -> dict[str, Any]:
    """Validate the fixed, data-only reference identity before any game calls."""
    keys = {"initial_observation_sha256", "save_directory", "df_version", "dfhack_version", "bridge_sha256", "protocol"}
    if not isinstance(value, dict) or set(value) not in (keys, keys | {"game_environment_sha256"}):
        raise ValueError("initial_expectation must contain the six native identity fields and optional game_environment_sha256")
    cleaned = dict(value)
    for key in ("initial_observation_sha256", "bridge_sha256", "game_environment_sha256"):
        if key not in value:
            continue
        cleaned[key] = _digest(value[key])
        if cleaned[key] is None:
            raise ValueError(f"initial_expectation.{key} must be a SHA-256 hex digest")
    for key, maximum in (("save_directory", 256), ("df_version", 128), ("dfhack_version", 128)):
        text = value[key]
        if (not isinstance(text, str) or not text.strip() or len(text) > maximum
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text)):
            raise ValueError(f"initial_expectation.{key} must be bounded, nonempty text without control characters")
    if type(value["protocol"]) is not int or not 1 <= value["protocol"] <= 2**31 - 1:
        raise ValueError("initial_expectation.protocol must be a positive integer")
    return cleaned


def _coherent_stream(events: list[dict[str, Any]], warn) -> bool:
    """Check the runner's envelope without inventing or reordering events."""
    coherent = True
    if [event.get("event") for event in events] != list(range(len(events))) or any(type(event.get("event")) is not int for event in events):
        warn("Event IDs are missing, duplicated, out of order, or not contiguous from zero.")
        coherent = False
    starts = [index for index, event in enumerate(events) if event.get("kind") == "run_start"]
    ends = [index for index, event in enumerate(events) if event.get("kind") == "run_end"]
    if starts != [0]:
        warn("Expected exactly one run_start record at the beginning of the stream.")
        coherent = False
    if not ends:
        warn("No run_end record; the saved episode is incomplete.")
        coherent = False
    elif ends != [len(events) - 1]:
        warn("Expected one final run_end record; duplicate endings or records after run_end are present.")
        coherent = False
    previous_wall = previous_turn = None
    for event in events:
        wall, turn = event.get("wall_seconds"), event.get("turn")
        if not _finite(wall) or wall < 0 or (previous_wall is not None and wall < previous_wall):
            warn("Event wall-clock offsets are missing, invalid, or move backwards.")
            coherent = False
        else:
            previous_wall = wall
        if type(turn) is not int or turn < 0 or (previous_turn is not None and turn < previous_turn):
            warn("Event turn numbers are missing, invalid, or move backwards.")
            coherent = False
        else:
            previous_turn = turn
        try:
            stamp = datetime.fromisoformat(event.get("at", ""))
            if stamp.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            warn("One or more event timestamps are missing or lack a time zone.")
            coherent = False
    return coherent


def _read(path: Path, limit: int = MAX_LOG_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular evidence file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Evidence file exceeds inspection limit: {path.name}")
    return data


def _model_input_contract(manifest, config, policy, events, warn):
    """Check a declared projection against raw snapshots; retain legacy records."""
    declarations = [container.get("model_observation_version") for container in (manifest, config, policy)]
    inputs = [event for event in events if event.get("kind") == "policy_input"]
    if all(value is None for value in declarations) and not any("model_observation_version" in event for event in inputs):
        return None, None
    version = manifest.get("model_observation_version")
    if version != MODEL_OBSERVATION_VERSION or any(value != version for value in declarations):
        warn("Model observation version is missing, unsupported, or inconsistent across recorded declarations.")
        return _version(version), False
    verified = True
    if manifest.get("model_observation") != projection_contract():
        warn("Model observation projection declaration disagrees with the recorded version.")
        verified = False
    native = None
    expected_content = None
    for event in events:
        if event.get("kind") == "snapshot":
            native = event.get("snapshot")
        elif event.get("kind") == "policy_input":
            try:
                projected = project_observation(native)
                encoded = model_input_bytes(projected, event.get("history"))
                valid = (event.get("observation") == projected and
                         event.get("model_observation_version") == version and
                         type(event.get("input_bytes")) is int and event["input_bytes"] == len(encoded) and
                         event.get("input_sha256") == hashlib.sha256(encoded).hexdigest())
                expected_content = encoded.decode("utf-8")
            except ModelObservationError:
                valid, expected_content = False, None
            if not valid:
                warn("A model policy input or its byte/hash record disagrees with the declared projection of the preceding native snapshot.")
                verified = False
        elif event.get("kind") == "policy_response":
            exchange = event.get("exchange")
            request = exchange.get("request") if isinstance(exchange, dict) else None
            if isinstance(request, dict):
                messages = request.get("messages")
                if (expected_content is None or not isinstance(messages, list) or len(messages) != 2 or
                        not isinstance(messages[1], dict) or messages[1].get("role") != "user" or
                        messages[1].get("content") != expected_content):
                    warn("A recorded model request does not match the exact canonical policy input.")
                    verified = False
    return version, verified


def _run(path: str | Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    try:
        manifest = _evidence_json(_read(root / "manifest.json", 4 * 1024 * 1024).decode("utf-8-sig"))
    except (RecursionError, _EvidenceDepthError):
        raise ValueError(f"Manifest JSON nesting exceeds the inspection limit in {root.name}") from None
    if not isinstance(manifest, dict):
        raise ValueError("Run manifest must be a JSON object")
    events = []
    warnings = []

    def warn(message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    raw_log = _read(root / "events.jsonl")
    lines = raw_log.splitlines(keepends=True)
    incomplete_tail = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = _evidence_json(line.decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("Event must be an object")
        except (RecursionError, _EvidenceDepthError):
            raise ValueError(f"Event JSON nesting exceeds the inspection limit in {root.name} at line {index + 1}") from None
        except (ValueError, UnicodeError):
            if index == len(lines) - 1 and not line.endswith((b"\n", b"\r")):
                warn("Incomplete final log record ignored; this run may have been interrupted.")
                incomplete_tail = True
                continue
            raise ValueError(f"Malformed complete event in {root.name} at line {index + 1}") from None
        events.append(value)
    starts = [event for event in events if event.get("kind") == "run_start"]
    if manifest.get("bridge") == "mock" or any(event.get("bridge") == "mock" or event.get("backend") == "mock" for event in starts):
        raise ValueError(f"{root.name} declares a mock backend; it is not a native experiment")
    snapshot_events = [event for event in events if event.get("kind") == "snapshot"]
    snapshots = [event["snapshot"] for event in snapshot_events
                 if isinstance(event.get("snapshot"), dict) and NATIVE_FIELDS <= event["snapshot"].keys()]
    if not snapshots:
        raise ValueError(f"{root.name} has no native experiment snapshots; mock scores cannot be compared here")
    if len(snapshots) != len(snapshot_events):
        raise ValueError(f"{root.name} contains a malformed or non-native snapshot schema")
    coherent = _coherent_stream(events, warn) and not incomplete_tail
    continuity = True
    prior_tick = None
    initial_save = snapshots[0].get("save_directory")
    for sample in snapshots:
        if any(sample.get(key) is not True for key in ("world_loaded", "map_loaded", "fortress_mode")):
            warn("Some samples do not confirm a loaded fortress world and map.")
            continuity = False
        if sample.get("paused") is not True:
            warn("Some native snapshots do not confirm the game was paused during observation.")
        tick = sample.get("absolute_tick")
        if type(tick) is not int or tick < 0 or (prior_tick is not None and tick < prior_tick):
            warn("Native game ticks are missing, invalid, or move backwards between snapshots.")
            continuity = False
        else:
            prior_tick = tick
        save = sample.get("save_directory")
        if not isinstance(save, str) or not save:
            warn("Native save-directory identity is missing; world continuity is unverified.")
            continuity = False
        elif save != initial_save:
            warn("Native save-directory identity changes during the run.")
            continuity = False
        if any(sample.get(key) != snapshots[0].get(key) for key in ("df_version", "dfhack_version")):
            warn("Game or DFHack version changes between native snapshots.")
            continuity = False
        if sample.get("errors") != []:
            warn("Native observation errors are present or their absence was not recorded.")
        citizens = sample.get("citizens")
        if not isinstance(citizens, list) or any(not isinstance(citizen, dict) for citizen in citizens):
            warn("Some citizen rosters are unavailable or malformed.")
        elif any(type(citizen.get("id")) is not int for citizen in citizens) or len({citizen["id"] for citizen in citizens}) != len(citizens):
            warn("Citizen IDs are missing, invalid, or duplicated within a native snapshot.")
        stock = sample.get("stocks")
        drink = stock.get("by_item_type", {}).get("DRINK") if isinstance(stock, dict) and isinstance(stock.get("by_item_type"), dict) else None
        if not isinstance(drink, dict) or not _finite(drink.get("stack_units")) or drink["stack_units"] < 0:
            warn("Some native drink-stack measurements are unavailable or invalid.")
    initial = snapshots[0]
    fingerprint = initial_observation_fingerprint(initial)
    ends = [event for event in events if event.get("kind") == "run_end"]
    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    if type(policy.get("is_model")) is not bool or (policy.get("is_model") is True and _version(policy.get("model")) is None):
        warn("Policy provenance does not explicitly identify a baseline or a named model.")
    if manifest.get("interface") != "native measurements + wait/brew/finish" or manifest.get("events_file") != "events.jsonl":
        warn("Native runner/interface provenance is missing or unrecognized in the manifest.")
    protocol = manifest.get("protocol") if type(manifest.get("protocol")) is int and manifest["protocol"] > 0 else None
    if protocol != PROTOCOL_VERSION:
        warn("Bridge protocol identity is missing or unsupported by this comparison tool.")
    bridge_hash = _digest(manifest.get("bridge_sha256"))
    if bridge_hash is None:
        warn("Bridge script SHA-256 identity is missing or invalid.")
    save_hash = _digest(config.get("starting_save_sha256"))
    if save_hash is None:
        warn("There is no declared starting-save identity with a valid SHA-256 digest.")
    if _digest(manifest.get("starting_save_sha256")) != save_hash:
        warn("Starting-save identity disagrees between the manifest and run configuration.")
    declared_initial = _digest(manifest.get("initial_snapshot_sha256"))
    if declared_initial is None:
        warn("Initial native snapshot integrity hash is missing or invalid.")
    elif declared_initial != hashlib.sha256(json_bytes(initial)).hexdigest():
        warn("Initial native snapshot does not match the hash recorded in the manifest.")
    if manifest.get("initial_tick") is not None and manifest["initial_tick"] != initial.get("absolute_tick"):
        warn("Initial native tick disagrees with the manifest.")
    if len(starts) == 1 and starts[0].get("config") != config:
        warn("Run-start configuration is missing or disagrees with the manifest.")
    if config.get("policy") != policy:
        warn("Policy configuration disagrees between the manifest's policy and run configuration.")
    model_observation_version, model_input_verified = _model_input_contract(manifest, config, policy, events, warn)
    scenario = config.get("scenario") if isinstance(config.get("scenario"), str) and config["scenario"] else None
    if scenario is None:
        warn("Scenario identity is missing from the recorded configuration.")
    limits = {}
    for key in COMPARISON_FIELDS:
        value = config.get(key)
        zero_allowed = key in ("max_total_ticks", "history_decisions")
        if key in ("max_wall_seconds", "request_timeout"):
            valid = _finite(value) and value > 0
        else:
            valid = type(value) is int and value >= (0 if zero_allowed else 1)
        if not valid:
            warn("One or more runner budgets, memory limits, or timing settings are missing or invalid.")
            limits[key] = None
        else:
            limits[key] = value
    requested_fps = config.get("simulation_fps")
    if requested_fps is not None and (type(requested_fps) is not int or not 1 <= requested_fps <= 10000):
        warn("Requested simulation FPS cap is invalid.")
        requested_fps = None
    limits["simulation_fps"] = requested_fps
    native_fps = initial.get("simulation_fps")
    effective_fps = native_fps.get("effective") if isinstance(native_fps, dict) else None
    limits["effective_simulation_fps"] = _reported_fps(effective_fps)
    if requested_fps is not None and limits["effective_simulation_fps"] != requested_fps:
        warn("Native observation does not confirm the requested simulation FPS cap.")
    end = ends[-1] if ends else {}
    if config.get("simulation_fps") is not None and end.get("speed_restored") is not True:
        warn("Restoration of the original simulation FPS cap was not confirmed.")
    outcome = end.get("outcome") if isinstance(end.get("outcome"), str) else None
    if outcome not in ("finished", "budget_exhausted"):
        warn(f"Terminal outcome is {outcome or 'unavailable'}; outcomes from this run need that qualification.")
    pause = end.get("pause_confirmed") if type(end.get("pause_confirmed")) is bool else None
    if pause is not True:
        warn("The final game pause was not confirmed in the terminal record.")
    if any(event.get("kind") in ("error", "cleanup_error") or (event.get("kind") == "action_result" and event.get("error")) for event in events):
        warn("The event stream records an action, run, or cleanup failure.")
    if ends and manifest.get("state") != "finished":
        warn("The manifest does not confirm a finished recording despite a run_end event.")
    if manifest.get("outcome") is not None and manifest["outcome"] != outcome:
        warn("Terminal outcome disagrees between the event stream and manifest.")
    if manifest.get("events") is not None and manifest["events"] != len(events):
        warn("Recorded event count disagrees with the manifest.")
    if manifest.get("event_bytes") is not None and manifest["event_bytes"] != len(raw_log):
        warn("Recorded log byte count disagrees with the manifest.")
    versions = {key: _version(initial.get(key)) for key in ("df_version", "dfhack_version")}
    if any(value is None for value in versions.values()):
        warn("Game or DFHack version is missing or invalid.")
    for key in versions:
        if manifest.get(key) is not None and manifest[key] != initial.get(key):
            warn("Game or DFHack version disagrees between the manifest and native observation.")
    expected = None
    guard = None
    environment_hash = None
    environment_inventory = manifest.get("game_environment")
    if environment_inventory is not None:
        from .environment import recorded_environment_fingerprint
        try:
            environment_hash = recorded_environment_fingerprint(environment_inventory)
        except (ValueError, TypeError, RecursionError):
            warn("Recorded installation environment inventory is malformed or its checksum disagrees.")
        environment_events = [event for event in events if event.get("kind") == "game_environment"]
        if (len(environment_events) != 1 or
                environment_events[0].get("fingerprint_sha256") != environment_hash):
            warn("Installation environment evidence is missing or inconsistent with the manifest.")
    guard_events = [event for event in events if event.get("kind") == "initial_state_check"]
    if manifest.get("initial_expectation") is not None:
        try:
            expected = validate_initial_expectation(manifest["initial_expectation"])
        except ValueError:
            warn("The declared initial-state expectation is malformed; reference matching is unverified.")
        if expected is not None:
            actual = {"initial_observation_sha256": fingerprint, "bridge_sha256": bridge_hash, "protocol": protocol}
            if "game_environment_sha256" in expected:
                actual["game_environment_sha256"] = environment_hash
            for key, maximum in (("save_directory", 256), ("df_version", 128), ("dfhack_version", 128)):
                value = initial.get(key)
                actual[key] = value if (isinstance(value, str) and value.strip() and len(value) <= maximum
                                       and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)) else None
            differences = [{"field": key, "expected": expected[key], "actual": actual[key]}
                           for key in sorted(expected) if expected[key] != actual[key]]
            guard = {"matched": not differences, "expected": expected, "actual": actual, "differences": differences}
            if differences:
                warn("Initial native state does not match the declared reference: " +
                     ", ".join(item["field"] for item in differences) + ".")
            recorded = ({key: guard_events[0].get(key) for key in guard} if len(guard_events) == 1 else None)
            if recorded != guard or manifest.get("initial_state_check") != guard:
                warn("Initial-state guard evidence is missing, duplicated, or disagrees with the recomputed reference check.")
            if len(starts) != 1 or starts[0].get("initial_expectation") != expected:
                warn("Run-start initial-state expectation is missing or disagrees with the manifest.")
            if len(ends) != 1 or end.get("initial_expectation") != expected or end.get("initial_state_check") != guard:
                warn("Terminal initial-state guard evidence is missing or inconsistent.")
            if len(guard_events) == 1:
                position = next(i for i, event in enumerate(events) if event is guard_events[0])
                initial_position = next(i for i, event in enumerate(events) if event.get("kind") == "snapshot")
                if position <= initial_position or any(
                        event.get("kind") in ("policy_input", "policy_response", "decision") or
                        (event.get("kind") == "action_result" and event.get("operation") in ("queue_brew", "advance_ticks"))
                        for event in events[:position]):
                    warn("The initial-state guard was not recorded after observation and before policy/gameplay actions.")
                if not guard["matched"] and any(event.get("kind") == "policy_input" for event in events):
                    warn("Policy calls were recorded despite a mismatched initial-state reference.")
    elif guard_events or manifest.get("initial_state_check") is not None:
        warn("An initial-state check was recorded without a declared reference expectation.")
    summary = summarize_events(events)
    if not continuity:
        for key in ("observed_tick_span", "population_change", "drink_stack_units_change", "observed_confirmed_deaths"):
            summary[key] = None
        summary["notes"].append("Cross-snapshot totals and changes are suppressed because native world/time continuity is unverified; endpoint records are retained.")
    label = policy.get("model") or policy.get("name") or policy.get("kind") or root.name
    return {
        "name": root.name, "policy": label, "policy_config": policy,
        "model_observation_version": model_observation_version,
        "model_input_contract_verified": model_input_verified,
        "complete_record": coherent, "warnings": warnings, "outcome": outcome, "pause_confirmed": pause,
        "world_time_continuity_verified": continuity,
        "declared_starting_save_sha256": save_hash,
        "initial_observation_sha256": fingerprint,
        "initial_expectation": expected, "initial_state_check": guard,
        "game_environment_sha256": environment_hash,
        "game_environment": environment_inventory,
        "versions": versions, "bridge_identity": {"protocol": protocol, "script_sha256": bridge_hash},
        "scenario": scenario, "limits": limits, "summary": summary,
    }


def read_run(path: str | Path) -> dict[str, Any]:
    """Read and validate one native recording, recomputing its care summary."""
    return _run(path)


def compare_runs(paths: list[str | Path]) -> dict[str, Any]:
    if len(paths) < 2:
        raise ValueError("Choose at least two native experiment directories")
    if len(paths) > 100:
        raise ValueError("Compare at most 100 runs at a time")
    if len({Path(path).expanduser().resolve() for path in paths}) != len(paths):
        raise ValueError("Choose distinct run directories; the same recording cannot count as two runs")
    runs = [_run(path) for path in paths]
    issues = []
    reference = runs[0]
    for run in runs:
        if run["warnings"]:
            issues.extend(f"{run['name']}: {warning}" for warning in run["warnings"])
    for run in runs[1:]:
        for key, label in (
            ("declared_starting_save_sha256", "declared starting-save identity"),
            ("initial_observation_sha256", "initial native observation"),
            ("versions", "game/DFHack versions"), ("limits", "run budgets and timing"),
            ("bridge_identity", "bridge script/protocol identity"), ("scenario", "scenario identity"),
            ("game_environment_sha256", "recorded installation configuration"),
            ("model_observation_version", "model observation input contract (unversioned historical input is unknown)"),
        ):
            if run[key] != reference[key]:
                issues.append(f"{run['name']} and {reference['name']}: different {label}")
    return {
        "schema_version": 1, "kind": "native_care_comparison", "runs": runs,
        "recorded_setup_matches": not issues, "comparability_issues": issues,
        "notes": [
            "Outcomes are recomputed from native snapshot events; stored result summaries are not trusted.",
            "A declared save identity does not prove the in-memory game was freshly restored. Initial observations are checked separately.",
            "Initial observation fingerprints exclude UI focus/pause and bridge-session history, while retaining native state and effective simulation/graphics caps. Unobserved game/RNG state is not verified.",
            "Matching recorded setup is not proof of deterministic simulation or statistical significance.",
            "Installation environment inventories cover their declared local file scope only. Older runs without inventories leave external configuration unverified, even when their other recorded conditions match.",
            "Policy/model identity and per-call response settings are reported separately; differing policies are expected in this comparison.",
            "Model observation projection versions are compared separately from policy identity. Historical unversioned inputs remain readable, with no verified projection contract; they are not interchangeable with versioned inputs.",
            "No composite wellbeing score or model ranking is produced; missing measurements remain unknown.",
        ],
    }


def render_comparison_report(report: dict[str, Any]) -> str:
    def show(value: Any) -> str:
        return "unknown" if value is None else str(value)
    lines = ["Native care outcomes (recomputed from recorded snapshots)",
             "Recorded setup: " + ("matches" if report["recorded_setup_matches"] else "differences or missing evidence"), ""]
    for run in report["runs"]:
        summary = run["summary"]
        first, last = summary["initial"], summary["final"]
        lines.extend([
            f"{run['name']} [{run['policy']}]",
            f"  Game ticks: {show(summary['observed_tick_span'])}; complete record: {run['complete_record']}",
            f"  Terminal outcome: {show(run['outcome'])}; final pause confirmed: {show(run['pause_confirmed'])}",
            f"  Population: {show(first['population'])} -> {show(last['population'])}; observed confirmed deaths: {show(summary['observed_confirmed_deaths'])}",
            f"  Drink stack units: {show(first['drink']['stack_units'])} -> {show(last['drink']['stack_units'])}",
        ])
        policy_config = run["policy_config"]
        if policy_config.get("is_model") is True:
            lines.append(f"  Policy call settings: response tokens {show(policy_config.get('max_completion_tokens'))}; field {show(policy_config.get('token_limit_field'))}")
        measures = last.get("citizen_measurements") or {}
        for key, label in (("stress", "Stress"), ("thirst_timer", "Thirst timer")):
            distribution = measures.get(key)
            if distribution:
                lines.append(f"  {label}: median {show(distribution['median'])}, maximum {show(distribution['max'])}, unknown {show(distribution['unknown'])}")
            else:
                lines.append(f"  {label}: unknown")
        lines.append("")
    if report["comparability_issues"]:
        lines.append("Comparison limits:")
        lines.extend("  - " + issue for issue in report["comparability_issues"])
    lines.extend(["", *report["notes"]])
    return "\n".join(lines)
