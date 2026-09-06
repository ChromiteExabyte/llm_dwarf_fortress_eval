"""Versioned, deterministic projection of native care measurements for policies.

This is a mechanical field projection, not a summary or welfare estimate. Raw
native snapshots remain the audit source. No values, totals or missing readings
are inferred. Complete repeated records use declared table columns; incomplete
records remain objects so an absent field stays distinct from an explicit null.
The projection never truncates citizens, needs, stock items or product receipts.
An oversized projection is rejected before a policy call.
"""

from __future__ import annotations

import json
from typing import Any


MODEL_OBSERVATION_VERSION = "native-care-v1"
MAX_MODEL_OBSERVATION_BYTES = 262144
NEED_COLUMNS = ("id", "type", "focus_level", "need_level", "deity_id")
STOCK_ITEM_COLUMNS = ("id", "item_type", "stack_size", "candidate", "in_job", "excluded_by")
_ROOT_FIELDS = frozenset({
    "absolute_tick", "year", "year_tick", "citizens", "known_former_citizens",
    "jobs", "workshops", "stocks", "brewing", "errors", "origin", "preflight",
})
_BREWING_OMISSIONS = frozenset({"session", "epoch", "notes"})


class ModelObservationError(ValueError):
    """Input cannot be represented by the declared projection."""


class ModelObservationTooLarge(ModelObservationError):
    """The complete projection exceeds its declared byte limit."""


def canonical_json_bytes(value: Any, *, max_bytes: int | None = None) -> bytes:
    """Serialize JSON deterministically without changing any numeric readings."""
    chunks, size = [], 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True,
                               separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if max_bytes is not None and size > max_bytes:
                raise ModelObservationTooLarge("Model observation exceeds max_model_observation_bytes; input was not sent")
            chunks.append(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ModelObservationError):
            raise
        raise ModelObservationError("Model observation must contain finite, serializable JSON values") from exc
    return b"".join(chunks)


def projection_contract() -> dict[str, Any]:
    """Return a fresh public declaration for manifests and inspection tools."""
    return {
        "version": MODEL_OBSERVATION_VERSION,
        "max_observation_bytes": MAX_MODEL_OBSERVATION_BYTES,
        "serialization": "UTF-8 JSON, sorted object keys, compact separators, finite numbers; native array order retained",
        "root_fields": sorted(_ROOT_FIELDS),
        "table_columns": {"needs": list(NEED_COLUMNS), "stock_items": list(STOCK_ITEM_COLUMNS)},
        "table_semantics": "Complete records are arrays in declared column order; incomplete or extended records stay objects. Null and absent fields are preserved.",
        "omissions": ["Unlisted root metadata including versions, save identity, pause/UI and simulation FPS",
                      "measurement_notes and stocks.definitions",
                      "brewing session, epoch and notes; product-event session and epoch"],
        "limits": "No row truncation, sampling, inferred values, welfare scores or external summaries. Oversized inputs stop before the policy call.",
    }


def _table(records: Any, columns: tuple[str, ...]) -> Any:
    if not isinstance(records, list):
        return records
    # Existing array rows are left intact, making projection idempotent. Their
    # column declaration is checked before accepting an already projected input.
    return [[record[key] for key in columns]
            if isinstance(record, dict) and set(record) == set(columns) else record
            for record in records]


def _citizens(records: Any) -> Any:
    if not isinstance(records, list):
        return records
    result = []
    for record in records:
        if isinstance(record, dict) and "needs" in record:
            record = {**record, "needs": _table(record["needs"], NEED_COLUMNS)}
        result.append(record)
    return result


def project_observation(observation: dict[str, Any], *,
                        max_bytes: int = MAX_MODEL_OBSERVATION_BYTES) -> dict[str, Any]:
    """Return an independent projection; reject rather than truncate at the cap.

    Both raw native snapshots and this version's own projected objects are
    accepted. A declared different version or table layout is rejected. Unknown
    root fields are omitted; values within retained measurement objects remain
    intact apart from the explicitly declared table and bookkeeping transforms.
    """
    if not isinstance(observation, dict):
        raise ModelObservationError("Model observation must be an object")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_MODEL_OBSERVATION_BYTES:
        raise ValueError("max_bytes must be a positive integer no larger than the projection cap")
    columns = {"needs": list(NEED_COLUMNS), "stock_items": list(STOCK_ITEM_COLUMNS)}
    if "model_observation_version" in observation:
        if observation["model_observation_version"] != MODEL_OBSERVATION_VERSION:
            raise ModelObservationError("Unsupported model observation version")
        if observation.get("table_columns") != columns:
            raise ModelObservationError("Model observation table columns do not match this version")
    projected = {key: value for key, value in observation.items() if key in _ROOT_FIELDS}
    projected.update(model_observation_version=MODEL_OBSERVATION_VERSION, table_columns=columns)
    for key in ("citizens", "known_former_citizens"):
        if key in projected:
            projected[key] = _citizens(projected[key])
    stocks = projected.get("stocks")
    if isinstance(stocks, dict):
        projected["stocks"] = {key: value for key, value in stocks.items() if key != "definitions"}
        if "items" in stocks:
            projected["stocks"]["items"] = _table(stocks["items"], STOCK_ITEM_COLUMNS)
    brewing = projected.get("brewing")
    if isinstance(brewing, dict):
        projected["brewing"] = {key: value for key, value in brewing.items() if key not in _BREWING_OMISSIONS}
        if isinstance(brewing.get("events"), list):
            projected["brewing"]["events"] = [
                {key: value for key, value in event.items() if key not in ("session", "epoch")}
                if isinstance(event, dict) else event for event in brewing["events"]]
    # Round-tripping creates an independent tree in canonical object-key order;
    # it does not normalize integers to floats, sort arrays, or fill missing keys.
    return json.loads(canonical_json_bytes(projected, max_bytes=max_bytes))


def model_input_bytes(observation: dict[str, Any], history: list[dict[str, Any]]) -> bytes:
    """Exact user-message bytes shared by the runner and both model transports."""
    if not isinstance(history, list):
        raise ModelObservationError("Public policy history must be a list")
    return canonical_json_bytes({"observation": project_observation(observation), "history": history})
