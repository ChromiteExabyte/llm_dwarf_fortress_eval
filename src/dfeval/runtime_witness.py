"""Bounded development-runtime evidence, owned by the supervisor.

Hash chaining detects edits relative to a retained head; it is not a signature
or an OS permission boundary. Never mount this directory into evaluated code.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .policies import strict_json


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


class WitnessError(RuntimeError):
    pass


class RuntimeWitness:
    """Single-writer journal. The broker serializes all writes under its lock."""

    def __init__(self, path: Path, max_bytes: int):
        self._file = path.open("xb")
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self.sequence = 0
        self.head = "0" * 64
        self.failed = False

    def record(self, kind: str, data: dict[str, Any]) -> None:
        if self.failed or self._file.closed:
            raise WitnessError("Witness is unavailable; runtime admission is closed")
        try:
            body = {"seq": self.sequence + 1, "previous_sha256": self.head,
                    "kind": kind, "at_unix_ns": time.time_ns(), "data": data}
            digest = hashlib.sha256(canonical(body)).hexdigest()
            line = canonical({**body, "sha256": digest}) + b"\n"
            if self.bytes_written + len(line) > self.max_bytes:
                raise WitnessError("Witness byte allowance exhausted")
            written = self._file.write(line)
            if written != len(line):
                raise WitnessError("Incomplete witness write")
            self._file.flush()
            os.fsync(self._file.fileno())
        except (OSError, ValueError, TypeError, RecursionError, WitnessError) as exc:
            self.failed = True
            raise WitnessError("Witness write failed; runtime admission is closed") from exc
        self.bytes_written += len(line)
        self.sequence += 1
        self.head = digest

    def close(self) -> None:
        self._file.close()


def verify_witness(path: Path, *, expected_head: str | None = None,
                   max_bytes: int = 512_000_000, max_line_bytes: int = 512_000_000) -> dict[str, Any]:
    """Stream-check evidence; a complete-looking chain alone cannot prove origin."""
    previous = "0" * 64
    count = total = 0
    last_kind = None
    with path.open("rb") as source:
        while line := source.readline(max_line_bytes + 1):
            total += len(line)
            if len(line) > max_line_bytes or total > max_bytes or not line.endswith(b"\n"):
                raise ValueError("Witness exceeds read limits or ends in an incomplete record")
            try:
                event = strict_json(line.decode("utf-8"))
                if not isinstance(event, dict) or set(event) != {
                        "seq", "previous_sha256", "kind", "at_unix_ns", "data", "sha256"}:
                    raise ValueError("Invalid witness record")
                digest = event.pop("sha256")
                if (type(event["seq"]) is not int or event["seq"] != count + 1
                        or event["previous_sha256"] != previous
                        or hashlib.sha256(canonical(event)).hexdigest() != digest):
                    raise ValueError("Witness chain mismatch")
            except (UnicodeError, RecursionError) as exc:
                raise ValueError("Invalid witness record") from exc
            previous, last_kind = digest, event["kind"]
            count += 1
    if not count or (expected_head is not None and expected_head != previous):
        raise ValueError("Witness is empty or does not match the retained head")
    return {"records": count, "bytes": total, "head_sha256": previous,
            "has_close_record": last_kind == "session_closed",
            "external_head_checked": expected_head is not None}
