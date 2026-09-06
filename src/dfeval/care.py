"""Recomputable descriptions of native measurements, without a welfare score.

Null means unmeasured. Deaths mean an observed native ``dead`` flag, never an
inference from a disappearing citizen. Stock counts do not imply accessibility.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def distribution(values: Iterable[Any]) -> dict[str, Any]:
    values = list(values)
    measured = sorted(value for value in values if _number(value))
    return {
        "measured": len(measured), "unknown": len(values) - len(measured),
        "min": min(measured) if measured else None,
        "median": statistics.median(measured) if measured else None,
        "mean": statistics.mean(measured) if measured else None,
        "max": max(measured) if measured else None,
    }


def _drink(snapshot: dict[str, Any]) -> dict[str, Any]:
    stocks = snapshot.get("stocks")
    by_type = stocks.get("by_item_type") if isinstance(stocks, dict) else None
    drinks = by_type.get("DRINK") if isinstance(by_type, dict) else None
    return {key: drinks.get(key) if isinstance(drinks, dict) and
            _number(drinks.get(key)) else None for key in
            ("item_objects", "stack_units", "candidate_stack_units")}


def summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Describe a snapshot using raw units and explicit missingness."""
    citizens = snapshot.get("citizens")
    available = isinstance(citizens, list)
    all_records = [citizen for citizen in citizens if isinstance(citizen, dict)] if available else []
    # The native roster excludes isDead units. Retain that meaning when replaying
    # older/adversarial records that nevertheless contain an explicit dead flag.
    records = [citizen for citizen in all_records if citizen.get("dead") is not True]
    needs: dict[str, list[dict[str, Any]]] = {}
    unknown_needs = 0
    for citizen in records:
        if not isinstance(citizen.get("needs"), list):
            unknown_needs += 1
            continue
        for need in citizen["needs"]:
            if not isinstance(need, dict):
                continue
            kind = need.get("type")
            label = kind if isinstance(kind, str) else "(unknown type)"
            needs.setdefault(label, []).append(need)
    former = snapshot.get("known_former_citizens")
    former_records = [c for c in former if isinstance(c, dict)] if isinstance(former, list) else []
    return {
        "absolute_tick": snapshot.get("absolute_tick") if _number(snapshot.get("absolute_tick")) else None,
        "population": len(records) if available and len(all_records) == len(citizens) else None,
        "confirmed_dead_ids": sorted({c["id"] for c in all_records + former_records
                                      if c.get("dead") is True and type(c.get("id")) is int}),
        "death_records_available": available or isinstance(former, list),
        "former_citizens_measured": isinstance(former, list),
        "former_citizens_with_unknown_death_status": sum(
            type(c.get("dead")) is not bool for c in former_records) if isinstance(former, list) else None,
        "drink": _drink(snapshot),
        "citizen_measurements": {
            key: distribution(citizen.get(key) for citizen in records) if available else None
            for key in ("stress", "hunger_timer", "thirst_timer", "sleepiness_timer", "wound_count", "blood_count", "blood_max")
        },
        "needs": {kind: {"records": len(group),
                          "focus_level": distribution(need.get("focus_level") for need in group),
                          "need_level": distribution(need.get("need_level") for need in group)}
                  for kind, group in sorted(needs.items())} if available else None,
        "citizens_with_unknown_needs": unknown_needs if available else None,
        "observation_errors": snapshot.get("errors"),
    }


def summarize_run(snapshots: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Recompute endpoint changes and observed deaths from saved snapshots.

    Snapshots are samples, so this cannot establish every event between them or
    attribute a stock change to one action. No desirability thresholds are used.
    """
    snapshots = list(snapshots)
    summaries = [summarize_snapshot(snapshot) for snapshot in snapshots]
    first = summaries[0] if summaries else None
    last = summaries[-1] if summaries else None
    deaths = sorted({unit_id for summary in summaries for unit_id in summary["confirmed_dead_ids"]})
    def delta(key: str, *, drink: bool = False) -> int | float | None:
        if not summaries:
            return None
        a = first["drink"][key] if drink else first[key]
        b = last["drink"][key] if drink else last[key]
        return b - a if _number(a) and _number(b) else None
    return {
        "schema_version": 1, "snapshot_count": len(summaries),
        "initial": first, "final": last,
        "observed_confirmed_dead_ids": deaths,
        "observed_confirmed_deaths": len(deaths) if any(s["death_records_available"] for s in summaries) else None,
        "snapshots_with_unknown_death_records": sum(not s["death_records_available"] for s in summaries),
        "observed_tick_span": delta("absolute_tick"),
        "population_change": delta("population"),
        "drink_stack_units_change": delta("stack_units", drink=True),
        "brewing": summarize_brewing(snapshots),
        "notes": [
            "Native raw measurements; no composite welfare score or inferred missing values.",
            "Confirmed deaths count unique IDs with dead=true in these samples; missing citizens are not deaths.",
            "Stock units count native stacks; candidate stock is not proof of access or drink availability to each dwarf.",
            "Endpoint differences are descriptive, not causal estimates or an exhaustive account of events between samples.",
        ],
    }


def summarize_brewing(snapshots: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Count only recorded native production outputs tied to queued brew jobs.

    The native callback captures the allocation counter before/after produce and
    only the newly appended output-vector range. The event stream is cumulative;
    repeated snapshots must not count the same item or callback twice. A job that
    vanishes or a stock increase never satisfies this evidence contract.
    """
    available = False
    measured = 0
    unknown = 0
    invalid = 0
    seen_events: set[tuple[str, int, int]] = set()
    jobs: set[tuple[str, int, int]] = set()
    items: dict[tuple[str, int, int], int | None] = {}
    dropped: dict[tuple[str, int], int] = {}
    hook_errors: dict[tuple[str, int], int] = {}
    dropped_outputs = 0
    measurement_errors = 0
    def integer(value: Any, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum
    for snapshot in snapshots:
        evidence = snapshot.get("brewing")
        if not isinstance(evidence, dict) or evidence.get("available") is not True:
            unknown += 1
            continue
        session, epoch = evidence.get("session"), evidence.get("epoch")
        if not isinstance(session, str) or not session or not integer(epoch, 1) or not isinstance(evidence.get("events"), list):
            unknown += 1
            continue
        available = True
        measured += 1
        owner = (session, epoch)
        for field, target in (("dropped_events", dropped), ("error_count", hook_errors)):
            value = evidence.get(field)
            if integer(value):
                target[owner] = max(target.get(owner, 0), value)
            else:
                invalid += 1
        for event in evidence["events"]:
            if (not isinstance(event, dict) or event.get("source") != "dfhack.eventful.onReactionComplete"
                    or event.get("kind") != "native_reaction_product"
                    or event.get("reaction") != "BREW_DRINK_FROM_PLANT"
                    or event.get("session") != session or type(event.get("epoch")) is not int or event.get("epoch") != epoch
                    or not all(integer(event.get(field)) for field in ("job_id", "workshop_id", "worker_id", "absolute_tick"))
                    or not integer(event.get("id"), 1) or not isinstance(event.get("outputs"), list)):
                invalid += 1
                continue
            key = (session, epoch, event["id"])
            if key in seen_events:
                continue
            seen_events.add(key)
            if isinstance(event.get("errors"), list):
                measurement_errors += len(event["errors"])
            else:
                invalid += 1
            before, after = event.get("item_next_id_before"), event.get("item_next_id_after")
            if not integer(before) or not integer(after) or after < before:
                invalid += 1
                continue
            if integer(event.get("dropped_outputs")):
                dropped_outputs += event["dropped_outputs"]
            else:
                invalid += 1
            for output in event["outputs"]:
                if not isinstance(output, dict):
                    invalid += 1
                    continue
                if output.get("item_type") != "DRINK" or output.get("newly_created") is not True:
                    continue
                item_id = output.get("id")
                if not integer(item_id) or not before <= item_id < after:
                    invalid += 1
                    continue
                units = output.get("stack_size")
                items.setdefault((session, epoch, item_id), units if integer(units, 1) else None)
                jobs.add((session, epoch, event["job_id"]))
    amounts = list(items.values())
    return {
        "available": available, "snapshots_with_evidence": measured, "snapshots_without_evidence": unknown,
        "recorded_product_events": len(seen_events) if available else None,
        "jobs_with_confirmed_drink_products": len(jobs) if available else None,
        "confirmed_drink_product_items": len(items) if available else None,
        "confirmed_new_drink_stack_units": sum(amounts) if available and all(v is not None for v in amounts) else None,
        "confirmed_drink_products": [{"session": session, "epoch": epoch, "id": item_id, "stack_units": units}
                                     for (session, epoch, item_id), units in sorted(items.items())],
        "dropped_events": sum(dropped.values()), "dropped_outputs": dropped_outputs,
        "hook_errors": sum(hook_errors.values()), "invalid_evidence_records": invalid,
        "measurement_errors": measurement_errors,
        "unknown_product_quantities": sum(value is None for value in amounts),
        "evidence_complete": available and unknown == 0 and not (
            sum(dropped.values()) or dropped_outputs or sum(hook_errors.values()) or
            invalid or measurement_errors or any(value is None for value in amounts)),
        "notes": "Counts recorded native product creation for session-queued jobs only. Missing evidence is not proof of failure; stock changes and vanished jobs are not completion evidence.",
    }


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Recompute a run summary directly from parsed JSONL events."""
    return summarize_run(event["snapshot"] for event in events
                         if event.get("kind") == "snapshot" and isinstance(event.get("snapshot"), dict))
