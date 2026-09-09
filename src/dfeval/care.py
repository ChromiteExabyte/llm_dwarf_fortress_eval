"""Recomputable descriptions of native measurements, without a welfare score.

Null means unmeasured. Deaths mean an observed native ``dead`` flag, never an
inference from a disappearing citizen. Stock counts do not imply accessibility.
"""

from __future__ import annotations

import math
import json
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


def summarize_run(snapshots: Iterable[dict[str, Any]], *, _receipts: dict[str, Any] | None = None) -> dict[str, Any]:
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
        "brewing": summarize_brewing(snapshots, _receipts=_receipts),
        "notes": [
            "Native raw measurements; no composite welfare score or inferred missing values.",
            "Confirmed deaths count unique IDs with dead=true in these samples; missing citizens are not deaths.",
            "Stock units count native stacks; candidate stock is not proof of access or drink availability to each dwarf.",
            "Endpoint differences are descriptive, not causal estimates or an exhaustive account of events between samples.",
        ],
    }


def summarize_brewing(snapshots: Iterable[dict[str, Any]], *, _receipts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Count only recorded native production outputs tied to queued brew jobs.

    The native callback captures the allocation counter before/after produce and
    only the newly appended output-vector range. The event stream is cumulative;
    repeated snapshots must not count the same item or callback twice. A job that
    vanishes or a stock increase never satisfies this evidence contract. Snapshot
    callers cannot verify queue receipts; summarize_events supplies that context.
    """
    available = False
    measured = 0
    unknown = 0
    invalid = 0
    records: dict[tuple[str, int, int], tuple[str, dict[str, Any], int, Any]] = {}
    conflicting_events: set[tuple[str, int, int]] = set()
    conflicting_items: set[tuple[str, int, int]] = set()
    counter_inconsistencies = 0
    previous_counters: dict[tuple[str, int], tuple[int, int, set[int]]] = {}
    jobs: set[tuple[str, int, int]] = set()
    items: dict[tuple[str, int, int], int | None] = {}
    item_callbacks: dict[tuple[str, int, int], tuple[str, int, int]] = {}
    dropped: dict[tuple[str, int], int] = {}
    hook_errors: dict[tuple[str, int], int] = {}
    dropped_outputs = 0
    measurement_errors = 0
    unknown_snapshot_clocks = 0
    clock_errors = 0
    unlinked_jobs: set[tuple[str, int, int]] = set()
    unknown_receipt_jobs: set[tuple[str, int, int]] = set()
    def integer(value: Any, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum
    for snapshot_index, snapshot in enumerate(snapshots):
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
        count, lost = evidence.get("event_count"), evidence.get("dropped_events")
        ids = [event.get("id") for event in evidence["events"]
               if isinstance(event, dict) and integer(event.get("id"), 1)]
        # The native hook appends IDs 1..N and drops only later events once full;
        # it never evicts earlier callbacks or resets counters within an epoch.
        counters_valid = (integer(count) and integer(lost)
                          and len(ids) == len(evidence["events"])
                          and sorted(ids) == list(range(1, len(ids) + 1))
                          and count == len(ids) + lost)
        previous = previous_counters.get(owner)
        if counters_valid and previous is not None:
            counters_valid = (count >= previous[0] and lost >= previous[1]
                              and previous[2].issubset(ids))
        if not counters_valid:
            counter_inconsistencies += 1
        if integer(count) and integer(lost):
            previous_counters[owner] = (count, lost, set(ids))
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
            try:
                canonical = json.dumps(event, sort_keys=True, allow_nan=False, separators=(",", ":"))
            except (ValueError, TypeError, RecursionError):
                invalid += 1
                continue
            if key in records:
                if records[key][0] != canonical:
                    conflicting_events.add(key)
                continue
            # Freeze the first representation for comparison, never as a winner
            # when a later cumulative snapshot contradicts the same callback.
            records[key] = (canonical, json.loads(canonical), snapshot_index, snapshot.get("absolute_tick"))
    for key, (_, event, snapshot_index, snapshot_tick) in records.items():
        if key in conflicting_events:
            continue
        session, epoch, _ = key
        if integer(snapshot_tick):
            if event["absolute_tick"] > snapshot_tick:
                clock_errors += 1
        else:
            unknown_snapshot_clocks += 1
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
            units = units if integer(units, 1) else None
            item_key = (session, epoch, item_id)
            # Repeated references inside one callback can describe one item.
            # Separate callbacks cannot both establish its native creation.
            if item_key in item_callbacks and item_callbacks[item_key] != key:
                conflicting_items.add(item_key)
            item_callbacks.setdefault(item_key, key)
            if item_key in items and items[item_key] != units:
                conflicting_items.add(item_key)
            else:
                items[item_key] = units
            job_key = (session, epoch, event["job_id"])
            jobs.add(job_key)
            if _receipts is not None:
                receipt = _receipts["jobs"].get(job_key)
                def contradicts(candidate: dict[str, Any]) -> bool:
                    return ((candidate["workshop_id"] is not None and candidate["workshop_id"] != event["workshop_id"])
                            or candidate["snapshot_index"] >= snapshot_index
                            or (integer(candidate["absolute_tick"]) and event["absolute_tick"] < candidate["absolute_tick"]))
                if receipt is None and job_key not in _receipts["jobs"]:
                    # A historical receipt with missing identifiers may be this
                    # job's acknowledgement. Known different IDs, workshops or
                    # times cannot explain an otherwise absent receipt.
                    possible = any((candidate["owner"] is None or candidate["owner"] == (session, epoch))
                                   and (candidate["job_ids"] is None or event["job_id"] in candidate["job_ids"])
                                   and not contradicts(candidate) for candidate in _receipts["unidentified"])
                    (unknown_receipt_jobs if possible else unlinked_jobs).add(job_key)
                elif receipt is None or contradicts(receipt):
                    unlinked_jobs.add(job_key)
                elif receipt["unknown"]:
                    unknown_receipt_jobs.add(job_key)
    for item_key in conflicting_items:
        items[item_key] = None
    amounts = list(items.values())
    quantities_complete = available and unknown == 0 and not (
        sum(dropped.values()) or dropped_outputs or sum(hook_errors.values()) or invalid
        or measurement_errors or any(value is None for value in amounts)
        or conflicting_events or conflicting_items or counter_inconsistencies or clock_errors)
    clock_verification = ("inconsistent" if clock_errors else
                          "unknown" if not available or unknown_snapshot_clocks else "verified")
    product_complete = quantities_complete and clock_verification == "verified"
    linkage_errors = None if _receipts is None else _receipts["errors"] + len(unlinked_jobs)
    unknown_receipts = None if _receipts is None else _receipts["unknown"]
    linkage = ("inconsistent" if linkage_errors else "unknown"
               if _receipts is None or unknown_receipts or unknown_receipt_jobs else "verified")
    complete = bool(product_complete and linkage == "verified")
    return {
        "available": available, "snapshots_with_evidence": measured, "snapshots_without_evidence": unknown,
        "recorded_product_events": len(records) if available else None,
        "jobs_with_confirmed_drink_products": len(jobs) if available else None,
        "confirmed_drink_product_items": len(items) if available else None,
        "confirmed_new_drink_stack_units": sum(amounts) if quantities_complete and linkage != "inconsistent" else None,
        "confirmed_drink_products": [{"session": session, "epoch": epoch, "id": item_id, "stack_units": units}
                                     for (session, epoch, item_id), units in sorted(items.items())],
        "dropped_events": sum(dropped.values()), "dropped_outputs": dropped_outputs,
        "hook_errors": sum(hook_errors.values()), "invalid_evidence_records": invalid,
        "measurement_errors": measurement_errors,
        "unknown_product_quantities": sum(value is None for value in amounts),
        "conflicting_product_events": len(conflicting_events),
        "conflicting_product_items": len(conflicting_items), "counter_inconsistencies": counter_inconsistencies,
        "product_clock_verification": clock_verification, "product_clock_errors": clock_errors,
        "product_events_with_unknown_snapshot_tick": unknown_snapshot_clocks,
        "receipt_linkage": linkage, "receipt_linkage_errors": linkage_errors,
        "unknown_queue_receipts": unknown_receipts,
        "product_jobs_with_unknown_receipt": len(unknown_receipt_jobs) if _receipts is not None else None,
        "unlinked_product_jobs": len(unlinked_jobs) if _receipts is not None else None,
        "product_evidence_complete": bool(product_complete), "evidence_complete": complete,
        "qualified_total": not complete,
        "notes": "Native product values are recorded measurements, not job completion or cancellation claims. "
                 "Snapshot-only receipt linkage is unknown. Conflicts or incomplete product counters suppress aggregate units; "
                 "missing snapshot clocks qualify timing, and callbacks beyond their first snapshot clock suppress aggregate units. "
                 "Full evidence also requires prior successful queue receipts and their preceding native clocks. "
                 "Missing receipt fields qualify linkage; contradictory fields suppress aggregate units. "
                 "Missing events, stock changes, and vanished jobs prove no outcome.",
    }


def _queue_receipts(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Bind queue acknowledgements to their preceding native observation epoch."""
    starts = sum(event.get("kind") == "run_start" for event in events)
    if not starts:
        return None  # A snapshot excerpt is not a complete queue ledger.
    receipts: dict[tuple[str, int, int], dict[str, Any] | None] = {}
    unidentified: list[dict[str, Any]] = []
    errors = int(starts != 1)
    unknown = 0
    owner, tick = None, None
    owner_invalid = False
    started = False
    snapshot_index = -1
    def integer(value: Any, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum
    for event in events:
        if event.get("kind") == "run_start":
            started = True
        if event.get("kind") == "snapshot" and isinstance(event.get("snapshot"), dict):
            if not started:
                errors += 1
            snapshot_index += 1
            sample = event["snapshot"]
            brewing = sample.get("brewing")
            owner, tick = None, sample.get("absolute_tick")
            owner_invalid = brewing is not None and not isinstance(brewing, dict)
            if isinstance(brewing, dict):
                session, epoch = brewing.get("session"), brewing.get("epoch")
                owner_invalid = ((session is not None and (not isinstance(session, str) or not session))
                                 or (epoch is not None and not integer(epoch, 1)))
            if (isinstance(brewing, dict) and isinstance(brewing.get("session"), str)
                    and brewing["session"] and integer(brewing.get("epoch"), 1)):
                owner = (brewing["session"], brewing["epoch"])
        if event.get("kind") != "action_result" or event.get("operation") != "queue_brew":
            continue
        if not started:
            errors += 1
            continue
        if event.get("error") is not None:
            continue  # A rejected queue is not an acceptance receipt.
        result, args = event.get("result"), event.get("arguments")
        if ((result is not None and not isinstance(result, dict))
                or (args is not None and not isinstance(args, dict))):
            errors += 1
            continue
        result, args = result or {}, args or {}
        ids = result.get("job_ids")
        workshop, requested_workshop = result.get("workshop_id"), args.get("workshop_id")
        queued, quantity = result.get("queued_jobs"), args.get("quantity")
        reaction, completed = result.get("reaction"), result.get("completed")
        invalid = owner_invalid or (tick is not None and not integer(tick))
        invalid = invalid or any(value is not None and not integer(value, minimum)
                                 for value, minimum in ((workshop, 0), (requested_workshop, 0), (queued, 1), (quantity, 1)))
        invalid = invalid or (ids is not None and (not isinstance(ids, list) or not 0 < len(ids) <= 10
                                                   or not all(integer(job) for job in ids) or len(set(ids)) != len(ids)))
        invalid = invalid or (reaction is not None and reaction != "BREW_DRINK_FROM_PLANT")
        invalid = invalid or (completed is not None and completed is not False)
        if not invalid:
            counts = [value for value in (len(ids) if ids is not None else None, queued, quantity) if value is not None]
            invalid = (len(set(counts)) > 1 or any(value > 10 for value in counts)
                       or (workshop is not None and requested_workshop is not None and workshop != requested_workshop))
        if invalid:
            errors += 1
            continue
        incomplete = any(value is None for value in (owner, tick, ids, workshop, requested_workshop,
                                                     queued, quantity, reaction, completed))
        unknown += int(incomplete)
        receipt = {"workshop_id": workshop if workshop is not None else requested_workshop,
                   "snapshot_index": snapshot_index, "absolute_tick": tick, "unknown": incomplete}
        if owner is None or ids is None:
            unidentified.append({**receipt, "owner": owner, "job_ids": ids})
            continue
        for job in ids:
            key = (*owner, job)
            if key in receipts:
                receipts[key] = None
                errors += 1
            else:
                receipts[key] = receipt
    return {"jobs": receipts, "unidentified": unidentified, "errors": errors, "unknown": unknown}


def summarize_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Recompute a run summary directly from parsed JSONL events."""
    events = list(events)
    snapshots = (event["snapshot"] for event in events
                 if event.get("kind") == "snapshot" and isinstance(event.get("snapshot"), dict))
    return summarize_run(snapshots, _receipts=_queue_receipts(events))
