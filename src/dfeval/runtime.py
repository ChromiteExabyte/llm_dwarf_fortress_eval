"""Shared inference and independent evidence for the future game sandbox.

This development component launches neither agent code nor Dwarf Fortress.
It is NOT an OS sandbox. Contexts belong to callers: there is no agent roster,
prompt injection, memory summarization, delegation policy, or hidden retry.
"""

from __future__ import annotations

import hashlib
import math
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .episode_spec import EpisodeSpec
from .inference import TextClient, TextInferenceError, TextRequest
from .runtime_witness import RuntimeWitness, WitnessError, canonical


@dataclass(frozen=True)
class RuntimeLimits:
    max_calls: int = 64
    max_output_tokens: int = 131072
    max_input_bytes: int = 16_000_000
    max_request_bytes: int = 262144
    max_in_flight: int = 4
    max_wall_seconds: float = 3600
    call_timeout: float = 300
    max_evidence_bytes: int = 256_000_000

    def __post_init__(self):
        for name, upper in (("max_calls", 100000), ("max_output_tokens", 10**9),
                            ("max_input_bytes", 10**10), ("max_request_bytes", 16_000_000),
                            ("max_in_flight", 32), ("max_evidence_bytes", 512_000_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"{name} must be an integer in 1..{upper}")
        for name in ("max_wall_seconds", "call_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
                raise ValueError(f"{name} must be finite and in (0, 86400]")
        if self.max_evidence_bytes < 65536:
            raise ValueError("max_evidence_bytes must be at least 65536")


class RuntimeRejected(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RuntimeBroker:
    """One fixed model, one resource account, many independent text contexts.

    Output limits are reserved atomically before dispatch. A reported completion
    count refunds unused reservation; absent/uncertain usage consumes the cap.
    Prompt bytes are bounded separately; tokens are not an equal-compute unit.
    A timeout closes admission because the provider may still be working.
    """

    def __init__(self, out: Path, client: TextClient, *, limits: RuntimeLimits | None = None,
                 objective: str = "Care for the dwarves", spec: EpisodeSpec | None = None):
        self.limits = limits or RuntimeLimits()
        if spec is not None:
            objective = spec.objective
        if type(objective) is not str or not objective.strip() or len(objective.encode("utf-8")) > 65536:
            raise ValueError("objective must be nonempty text of at most 65536 UTF-8 bytes")
        # Snapshot trusted configuration before creating any output.
        provider = client.public_config()
        manifest = {"version": 1, "kind": "inference_runtime_development",
                    "session_id": uuid.uuid4().hex, "objective": objective,
                    "episode_spec": spec.to_dict() if spec else None,
                    "provider": provider, "limits": asdict(self.limits),
                    "isolation": "none", "game_connected": False,
                    "executes_code": False, "initial_workspace": "empty",
                    "context_management": "caller_supplied_messages",
                    "output_accounting": "successful_reported_completion_tokens_else_requested_cap"}
        client.check_public_input(manifest)
        manifest_raw = canonical(manifest)
        self._manifest = manifest_raw
        self.out = Path(out).absolute()
        self.out.mkdir(parents=True, exist_ok=False)
        self.workspace = self.out / "workspace"
        self.workspace.mkdir()
        evidence = self.out / "evidence"
        evidence.mkdir()
        (evidence / "manifest.json").write_bytes(manifest_raw + b"\n")
        self._witness = RuntimeWitness(evidence / "events.jsonl", self.limits.max_evidence_bytes)
        self._client = client
        self._lock = threading.Condition(threading.RLock())
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._closed = False
        self._fatal: str | None = None
        self._provider_activity_unknown = False
        self._calls = self._input_bytes = self._charged_output = self._active = 0
        self._evidence_reserved = 0
        self._reported = {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0}
        self._unknown = {key: 0 for key in self._reported}
        self._witness_seconds = 0.0
        try:
            self._record("session_opened", {"manifest": manifest,
                         "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()})
        except BaseException:
            self._witness.close()
            raise

    def _record(self, kind: str, data: dict[str, Any]) -> None:
        started = time.monotonic()
        try:
            self._witness.record(kind, data)
        except WitnessError:
            self._fatal = "witness_failure"
            self._stop.set()
            raise
        finally:
            self._witness_seconds += time.monotonic() - started

    def status(self) -> dict[str, Any]:
        with self._lock:
            elapsed = time.monotonic() - self._started
            return {"calls_admitted": self._calls, "in_flight": self._active,
                    "input_bytes_charged": self._input_bytes,
                    "output_tokens_charged_or_reserved": self._charged_output,
                    "reported_usage_totals": dict(self._reported),
                    "calls_with_unknown_usage": dict(self._unknown),
                    "remaining": {"calls": self.limits.max_calls - self._calls,
                                  "input_bytes": self.limits.max_input_bytes - self._input_bytes,
                                  "output_tokens": self.limits.max_output_tokens - self._charged_output,
                                  "wall_seconds": max(0, self.limits.max_wall_seconds - elapsed)},
                    "wall_seconds": elapsed, "witness_seconds": self._witness_seconds,
                    "closed": self._closed, "admission_stopped": self._stop.is_set(),
                    "provider_activity_may_continue": self._provider_activity_unknown,
                    "fatal_error": self._fatal, "witness_head_sha256": self._witness.head}

    def description(self) -> dict[str, Any]:
        # Do not expose provider endpoint, credential variable, evidence, or host paths.
        import json
        manifest = json.loads(self._manifest)
        return {"version": 1, "objective": manifest["objective"],
                "model": manifest["provider"]["model"], "limits": asdict(self.limits),
                "isolation": "none", "game_connected": False, "executes_code": False,
                "messages": "Caller supplies the entire context; no messages are added.",
                "inference": {"method": "POST", "path": "/v1/infer",
                              "fields": {"messages": "array of role/content text objects",
                                         "max_output_tokens": "positive integer",
                                         "temperature": "optional number"}},
                "resources": self.status()}

    def infer(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or not {"messages", "max_output_tokens"} <= value.keys() or value.keys() - {
                "messages", "max_output_tokens", "temperature"}:
            raise RuntimeRejected("invalid_request", "Expected messages, max_output_tokens and optional temperature")
        try:
            req = TextRequest(**value)
            # Canonicalized request bytes, not token estimates or transport whitespace.
            payload = req.to_dict()
            size = len(canonical(payload))
            self._client.check_public_input(payload)
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise RuntimeRejected("invalid_request", "Invalid text inference request") from exc
        # Reserve enough disk for escaped request, raw response and decoded text.
        # Leave a terminal record margin; do this before contacting a provider.
        evidence_reservation = 12 * (size + self._client.max_response_bytes) + 65536
        with self._lock:
            remaining = self.limits.max_wall_seconds - (time.monotonic() - self._started)
            if self._stop.is_set() or self._closed:
                raise RuntimeRejected("closed", "Runtime admission is closed")
            if remaining <= 0:
                raise RuntimeRejected("wall_limit", "Runtime wall-time allowance exhausted")
            if size > self.limits.max_request_bytes:
                raise RuntimeRejected("request_limit", "Request exceeds byte allowance")
            if req.max_output_tokens > self._client.max_output_tokens:
                raise RuntimeRejected("request_limit", "Request exceeds provider output cap")
            if self._active >= self.limits.max_in_flight:
                raise RuntimeRejected("busy", "Concurrent inference allowance is in use")
            if self._calls >= self.limits.max_calls or self._input_bytes + size > self.limits.max_input_bytes or (
                    self._charged_output + req.max_output_tokens > self.limits.max_output_tokens):
                raise RuntimeRejected("resource_limit", "Shared inference allowance exhausted")
            if self._witness.bytes_written + self._evidence_reserved + evidence_reservation + 65536 > self.limits.max_evidence_bytes:
                raise RuntimeRejected("evidence_limit", "Insufficient witness capacity for another exchange")
            call_id = self._calls + 1
            # A failed write prevents dispatch and poisons the session.
            self._record("inference_reserved", {"call_id": call_id, "request": payload,
                         "request_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
                         "input_bytes": size, "reserved_output_tokens": req.max_output_tokens})
            self._calls += 1
            self._input_bytes += size
            self._charged_output += req.max_output_tokens
            self._active += 1
            self._evidence_reserved += evidence_reservation
            timeout = min(self.limits.max_wall_seconds - (time.monotonic() - self._started),
                          self.limits.call_timeout, self._client.timeout)

        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def invoke():
            try:
                result_queue.put((self._client.infer(req, timeout=timeout), None))
            except Exception as exc:
                result_queue.put((None, exc))

        started = time.monotonic()
        worker = threading.Thread(target=invoke, daemon=True)
        result = failure = None
        disposition = "completed"
        try:
            if timeout > 0 and not self._stop.is_set():
                worker.start()
            while True:
                left = timeout - (time.monotonic() - started)
                if self._stop.is_set() or left <= 0:
                    disposition = "abandoned" if self._stop.is_set() else "timeout"
                    break
                try:
                    result, failure = result_queue.get(timeout=min(left, 0.05))
                    if time.monotonic() - started > timeout:
                        disposition = "timeout"
                        result = failure = None
                    break
                except queue.Empty:
                    pass
        except Exception as exc:
            failure = exc
        elapsed = time.monotonic() - started
        with self._lock:
            try:
                if disposition != "completed" or (failure is not None and (
                        not isinstance(failure, TextInferenceError) or failure.provider_may_be_running)):
                    self._fatal = self._fatal or "provider_activity_unknown"
                    self._provider_activity_unknown = True
                    self._stop.set()
                exchange = result.get("exchange") if isinstance(result, dict) else (
                    failure.exchange if isinstance(failure, TextInferenceError) else None)
                usage = result.get("usage") if isinstance(result, dict) else (
                    exchange.get("usage") if isinstance(exchange, dict) else None)
                usage = usage if isinstance(usage, dict) else {}
                count = usage.get("completion_tokens")
                known_count = type(count) is int and 0 <= count <= req.max_output_tokens
                if count is not None and not known_count:
                    self._fatal = "provider_usage_exceeds_reservation"
                    self._stop.set()
                    disposition = "invalid_usage"
                charge = count if known_count and disposition == "completed" and failure is None else req.max_output_tokens
                self._charged_output -= req.max_output_tokens - charge
                for key in self._reported:
                    number = usage.get(key)
                    if type(number) is int and number >= 0:
                        self._reported[key] += number
                    else:
                        self._unknown[key] += 1
                outcome = {"call_id": call_id, "outcome": disposition if disposition != "completed" else (
                           "provider_error" if failure else "completed"),
                           "provider_wall_seconds": elapsed, "charged_output_tokens": charge,
                           "exchange": exchange, "usage": usage or None,
                           "error": str(failure) if isinstance(failure, TextInferenceError) else (
                               "Provider call failed" if failure else None)}
                # Response becomes observable to callers only after its evidence is durable.
                self._record("inference_finished", outcome)
            finally:
                self._active -= 1
                self._evidence_reserved -= evidence_reservation
                self._lock.notify_all()
        if disposition != "completed" or failure is not None:
            raise RuntimeRejected(disposition if disposition != "completed" else "provider_error",
                                  "Inference did not complete; inspect supervisor evidence")
        return {"call_id": call_id, "text": result["text"],
                "finish_reason": result.get("finish_reason"), "usage": result.get("usage"),
                "timing": result.get("timing"), "resources": self.status()}

    def close(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return self.status()
            self._stop.set()
            while self._active:
                self._lock.wait(timeout=0.1)
            if self._closed:
                return self.status()
            try:
                self._record("session_closed", {"resources": self.status(),
                             "provider_activity_may_continue": self._provider_activity_unknown})
            finally:
                self._closed = True
                self._witness.close()
            return self.status()
