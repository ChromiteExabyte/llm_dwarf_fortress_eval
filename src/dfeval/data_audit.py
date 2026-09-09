"""Audit captured native evidence and locate sampled differences between runs.

Every analysis uses the same bounded copy of the source bytes. The originals
are read again before returning so a growing recording cannot quietly supply
different data to different checks. This is evidence inspection, not a model
judge, simulation replay, or proof that a recording's author is trustworthy.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable

from . import comparison
from .action_evidence import audit_action_evidence
from .benchmark_report import _events
from .policies import DecisionError, validate_decision


MANIFEST_LIMIT = 4 * 1024 * 1024
MAX_DIFFERENCES = 64
MAX_DISPLAY_BYTES = 512
MAX_POINTER_BYTES = 512
NOTES = [
    "This checks recorded facts and their internal agreement; it supplies no definition or grade of care.",
    "A validated decision, a dispatched request, a queue receipt, and native product creation are distinct facts. Missing completion or cancellation evidence stays unknown.",
    "Matching game requests does not imply matching model inputs: public history includes the model's reasons and notebook.",
    "Native states align only at unique recorded absolute ticks. A first differing sample bounds when a measured difference appeared; it does not identify the first divergent simulation tick or its cause.",
    "The native comparison uses the existing initial-state fingerprint projection. UI focus/pause, former-citizen history, brewing session/product history, and FPS override bookkeeping are excluded; native measurements and unknown fields remain.",
    "A common action sequence and two recordings do not establish determinism, a noise floor, or a model effect. Inspect start identities, settings, gaps, and evidence qualifications.",
    "Hashes identify the inspected bytes; they do not authenticate an author or prove the state of an unobserved game or RNG.",
]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _display(value: Any) -> Any:
    raw = _canonical(value)
    if len(raw) <= MAX_DISPLAY_BYTES:
        return value
    return {"value_omitted": True, "canonical_bytes": len(raw), "sha256": _hash(raw)}


def _pointer(segments: tuple[str, ...]) -> dict[str, Any]:
    """Render a bounded exact JSON pointer, hashing oversized paths incrementally.

    Imported JSON can contain lone escaped surrogates. Surrogate-pass encoding
    preserves those code points in the byte identity without inventing text.
    Ordinary Unicode uses its usual UTF-8 bytes.
    """
    digest = hashlib.sha256()
    size = 0
    retained: list[str] = []

    def add(piece: str) -> None:
        nonlocal size
        encoded = piece.encode("utf-8", errors="surrogatepass")
        digest.update(encoded)
        size += len(encoded)
        if size <= MAX_POINTER_BYTES:
            retained.append(piece)
        else:
            retained.clear()

    for segment in segments:
        add("/")
        for start in range(0, len(segment), 4096):
            add(segment[start:start + 4096].replace("~", "~0").replace("/", "~1"))
    if size <= MAX_POINTER_BYTES:
        return {"path": "".join(retained)}
    return {"path": None, "path_omitted": True, "path_bytes": size,
            "path_sha256": digest.hexdigest(), "path_encoding": "utf-8-surrogatepass"}


def _differences(left: Any, right: Any) -> dict[str, Any]:
    """JSON-pointer differences, retaining missing versus explicit null."""
    found = []
    count = 0
    # Segments retain references to source keys instead of copying a potentially
    # enormous common prefix into every queued descendant.
    pending = [((), True, left, True, right)]
    while pending:
        path, has_left, before, has_right, after = pending.pop()
        if has_left and has_right and type(before) is dict and type(after) is dict:
            for key in sorted(before.keys() | after.keys(), reverse=True):
                pending.append((path + (key,), key in before, before.get(key), key in after, after.get(key)))
            continue
        if has_left and has_right and type(before) is list and type(after) is list:
            for index in range(max(len(before), len(after)) - 1, -1, -1):
                pending.append((path + (str(index),), index < len(before),
                                before[index] if index < len(before) else None, index < len(after),
                                after[index] if index < len(after) else None))
            continue
        if has_left == has_right and _canonical(before) == _canonical(after):
            continue
        count += 1
        if len(found) < MAX_DIFFERENCES:
            found.append({**_pointer(path), "left_present": has_left, "right_present": has_right,
                          "left": _display(before) if has_left else None,
                          "right": _display(after) if has_right else None})
    return {"differences": found, "difference_count": count, "differences_truncated": count > len(found)}


def _sequence(events: list[dict[str, Any]], *, game_requests: bool = False) -> list[dict[str, Any]]:
    rows = []
    previous_tick = None
    for event in events:
        if event.get("kind") == "snapshot":
            previous_tick = event.get("snapshot", {}).get("absolute_tick")
        if game_requests:
            if event.get("kind") != "action_result" or event.get("operation") not in ("queue_brew", "advance_ticks"):
                continue
            value = {"operation": event["operation"], "arguments": event.get("arguments"),
                     "preceding_snapshot_tick": previous_tick}
        else:
            if event.get("kind") != "decision":
                continue
            try:
                decision = validate_decision(event.get("decision"))
            except DecisionError:
                rows.append({"event": event.get("event"), "turn": event.get("turn"), "valid": False, "value": None})
                continue
            value = {key: decision[key] for key in ("action", "workshop_id", "quantity") if key in decision}
        rows.append({"event": event.get("event"), "turn": event.get("turn"), "valid": True, "value": value})
    return rows


def _compare_sequence(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    valid = all(row["valid"] for row in left + right)
    left_values, right_values = ([row["value"] for row in rows] for rows in (left, right))
    first = None
    for index in range(max(len(left), len(right))):
        before = left[index] if index < len(left) else None
        after = right[index] if index < len(right) else None
        if before is None or after is None or _canonical(before["value"]) != _canonical(after["value"]):
            first = {"index": index, "left": before, "right": after}
            break
    return {"equal": first is None if valid else None, "left_count": len(left), "right_count": len(right),
            "left_sha256": _hash(_canonical(left_values)) if valid else None,
            "right_sha256": _hash(_canonical(right_values)) if valid else None,
            "first_difference": first}


def _native_difference(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    def samples(events):
        result = {}
        invalid = []
        for event in events:
            if event.get("kind") != "snapshot":
                continue
            snapshot = event.get("snapshot", {})
            tick = snapshot.get("absolute_tick")
            if type(tick) is not int or tick < 0:
                invalid.append(event.get("event"))
            else:
                result.setdefault(tick, []).append(event)
        return result, invalid

    a, invalid_a = samples(left)
    b, invalid_b = samples(right)
    ambiguous = sorted(tick for tick in a.keys() | b.keys() if len(a.get(tick, [])) > 1 or len(b.get(tick, [])) > 1)
    shared = sorted(tick for tick in a.keys() & b.keys() if len(a[tick]) == len(b[tick]) == 1)
    first = last_equal = None
    unequal_count = 0
    for tick in shared:
        before, after = a[tick][0], b[tick][0]
        before_state = comparison.comparable_native_state(before["snapshot"])
        after_state = comparison.comparable_native_state(after["snapshot"])
        before_hash, after_hash = (_hash(_canonical(value)) for value in (before_state, after_state))
        reference = {"absolute_tick": tick, "left_event": before.get("event"), "right_event": after.get("event")}
        if before_hash == after_hash:
            if first is None:
                last_equal = reference
        else:
            unequal_count += 1
            if first is None:
                first = {**reference, "left_sha256": before_hash, "right_sha256": after_hash,
                         **_differences(before_state, after_state)}
    gaps = bool(a.keys() ^ b.keys() or ambiguous or invalid_a or invalid_b)
    return {"alignment": "unique absolute_tick", "shared_sample_count": len(shared),
            "differing_sample_count": unequal_count,
            "equal_at_shared_samples": unequal_count == 0 if shared else None,
            "equal_complete_sampled_trajectory": False if unequal_count else None if gaps or not shared else True,
            "last_equal_sample_before_difference": last_equal if first else None,
            "first_observed_divergence": first,
            "left_only_ticks": sorted(a.keys() - b.keys()), "right_only_ticks": sorted(b.keys() - a.keys()),
            "ambiguous_ticks": ambiguous, "left_invalid_tick_events": invalid_a, "right_invalid_tick_events": invalid_b}


def audit_runs(run_dirs: Iterable[str | Path]) -> dict[str, Any]:
    """Inspect one recording or compare two; never restore a save or run a model."""
    if isinstance(run_dirs, (str, bytes, Path)):
        raise ValueError("Pass one or two run directories")
    paths = []
    for path in run_dirs:
        paths.append(Path(path).expanduser().resolve())
        if len(paths) > 2:
            raise ValueError("Audit one recording or compare two")
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("Choose one or two distinct run directories")
    captured, frozen, records, runs = [], [], [], []
    with ExitStack() as stack:
        for path in paths:
            raw = {name: comparison._read(path / name, limit) for name, limit in
                   (("manifest.json", MANIFEST_LIMIT), ("events.jsonl", comparison.MAX_LOG_BYTES))}
            copied = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dfeval-audit-"))).resolve()
            for name, content in raw.items():
                (copied / name).write_bytes(content)
            captured.append(raw)
            frozen.append(copied)
            manifest = comparison._evidence_json(raw["manifest.json"].decode("utf-8-sig"))
            # read_run validates native provenance and parses the identical bytes.
            checked = comparison.read_run(copied)
            events = _events(raw["events.jsonl"])
            records.append(events)
            actions = audit_action_evidence(events, checked["policy_config"], manifest.get("config", {}))
            runs.append({"name": path.name,
                         "source_files": [{"name": name, "bytes": len(content), "sha256": _hash(content)} for name, content in raw.items()],
                         "record": {key: checked.get(key) for key in (
                             "complete_record", "outcome", "pause_confirmed", "world_time_continuity_verified",
                             "model_observation_version", "model_input_contract_verified", "declared_starting_save_sha256",
                             "initial_observation_sha256", "game_environment_sha256", "warnings")},
                         "action_evidence": actions, "production_evidence": checked["summary"]["brewing"]})
        paired = None
        if len(paths) == 2:
            setup = comparison.compare_runs(frozen)
            issues = list(setup["comparability_issues"])
            for index, directory in enumerate(frozen):
                issues = [issue.replace(directory.name, paths[index].name) for issue in issues]
            decisions = _compare_sequence(*[_sequence(events) for events in records])
            requests = _compare_sequence(*[_sequence(events, game_requests=True) for events in records])
            full_decisions = [[event.get("decision") for event in events if event.get("kind") == "decision"] for events in records]
            paired = {"recorded_setup_matches": setup["recorded_setup_matches"], "comparability_issues": issues,
                      "decision_actions": decisions, "game_action_requests": requests,
                      "full_public_decisions_equal": _canonical(full_decisions[0]) == _canonical(full_decisions[1]),
                      "native_state": _native_difference(*records)}
        for path, raw in zip(paths, captured):
            for name, content in raw.items():
                limit = MANIFEST_LIMIT if name == "manifest.json" else comparison.MAX_LOG_BYTES
                if comparison._read(path / name, limit) != content:
                    raise ValueError("Recording changed during audit; stop the recorder and retry: " + path.name)
    return {"schema_version": 1, "kind": "native_data_audit", "runs": runs, "comparison": paired, "notes": list(NOTES)}


def render_audit(report: dict[str, Any]) -> str:
    def status(value):
        return "verified" if value is True else "inconsistent" if value is False else "unknown"

    lines = []
    for run in report["runs"]:
        record, actions, products = run["record"], run["action_evidence"], run["production_evidence"]
        lines.extend([run["name"], f"  Recorded outcome: {record['outcome'] or 'unknown'}",
                      f"  Model input contract: {status(record['model_input_contract_verified'])}",
                      f"  Response / action evidence: {status(actions['verified'])}",
                      f"  Product receipt linkage: {products.get('receipt_linkage', 'unknown')}"])
        for issue in actions.get("issues", [])[:10]:
            lines.append(f"  {issue.get('severity', 'unknown')}: {issue.get('message', issue.get('code'))}")
        if len(actions.get("issues", [])) > 10:
            lines.append("  Additional action issues are available with --json.")
        lines.extend("  Evidence qualification: " + warning for warning in record["warnings"])
    pair = report.get("comparison")
    if pair:
        lines.extend(["Comparison", f"  Same recorded decision actions: {pair['decision_actions']['equal']}",
                      f"  Same logged game requests and preceding ticks: {pair['game_action_requests']['equal']}",
                      f"  Same full public decisions: {pair['full_public_decisions_equal']}"])
        native = pair["native_state"]
        first = native["first_observed_divergence"]
        if first:
            lines.append(f"  First differing native sample: tick {first['absolute_tick']} (events {first['left_event']} / {first['right_event']})")
            for item in first["differences"][:10]:
                pointer = item["path"] if isinstance(item.get("path"), str) else (
                    f"[pointer omitted: {item.get('path_bytes', 'unknown')} bytes; SHA-256 {item.get('path_sha256', 'unknown')}]")
                lines.append(f"    {pointer}: {item['left']!r} -> {item['right']!r}")
        else:
            lines.append(f"  No difference found across {native['shared_sample_count']} uniquely aligned native samples.")
        if native["left_only_ticks"] or native["right_only_ticks"] or native["ambiguous_ticks"]:
            lines.append("  Sample gaps or duplicate ticks prevent a complete trajectory match; inspect --json.")
        lines.extend("  Comparison qualification: " + issue for issue in pair["comparability_issues"])
    lines.append("Use --json for event references, source hashes, per-turn evidence, and comparison limits. This audit supplies no care score.")
    return "\n".join(lines)
