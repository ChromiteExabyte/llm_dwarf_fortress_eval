"""Data-only conditions for a future episode beginning at an undeveloped embark.

These declarations neither enforce an information boundary nor establish that a
checkpoint is undeveloped. Model/provider settings, finite resource limits, and
fresh-session records belong to a separate runtime binding. No game or process
is launched here, and no care rubric, journal, or strategy is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any


DEFAULT_OBJECTIVE = "Care for the dwarves"
SCHEMA_VERSION = 1
MAX_SPEC_BYTES = 32 * 1024
MAX_OBJECTIVE_BYTES = 4 * 1024

_FIXED_FIELDS = {
    "schema_version": SCHEMA_VERSION,
    "starting_state": "undeveloped_embark",
    "declaration_basis": "operator_declared_unverified",
    "pause_control": "model",
}
_FIELDS = frozenset(_FIXED_FIELDS) | {
    "starting_checkpoint_sha256", "information_condition", "control_disclosure",
    "memory_writes", "objective",
}


class EpisodeSpecError(ValueError):
    """The episode declaration is malformed or a required choice is unresolved."""


def _utf8(value: str, limit: int, label: str) -> bytes:
    if type(value) is not str:
        raise EpisodeSpecError(f"{label} must be a string")
    if len(value) > limit:
        raise EpisodeSpecError(f"{label} exceeds its byte limit")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EpisodeSpecError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > limit:
        raise EpisodeSpecError(f"{label} exceeds its byte limit")
    return encoded


@dataclass(frozen=True, slots=True)
class EpisodeSpec:
    """Immutable scalar declarations, reusable independently of any model run.

    ``memory_writes`` concerns direct modification of game memory/state, not the
    agent's own notes or tools. Information and disclosure choices are required;
    an unresolved memory-write choice is useful while designing an experiment.
    """

    starting_checkpoint_sha256: str
    information_condition: str
    control_disclosure: str
    memory_writes: str = "unresolved"
    objective: str = DEFAULT_OBJECTIVE
    schema_version: int = field(default=SCHEMA_VERSION, init=False)
    starting_state: str = field(default="undeveloped_embark", init=False)
    declaration_basis: str = field(default="operator_declared_unverified", init=False)
    pause_control: str = field(default="model", init=False)

    def __post_init__(self) -> None:
        if (type(self.starting_checkpoint_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.starting_checkpoint_sha256) is None):
            raise EpisodeSpecError("starting_checkpoint_sha256 must be 64 lowercase hexadecimal characters")
        for name, choices in (
            ("information_condition", ("player", "privileged")),
            ("control_disclosure", ("documented", "undocumented")),
            ("memory_writes", ("unresolved", "denied", "allowed")),
        ):
            value = getattr(self, name)
            if type(value) is not str or value not in choices:
                raise EpisodeSpecError(f"Invalid {name}")
        _utf8(self.objective, MAX_OBJECTIVE_BYTES, "objective")
        if not self.objective.strip():
            raise EpisodeSpecError("objective must contain non-whitespace text")

    def to_dict(self) -> dict[str, Any]:
        """Return a new JSON object containing every declared field."""
        return {name: getattr(self, name) for name in sorted(_FIELDS)}

    def canonical_bytes(self) -> bytes:
        """Return version 1's compact, sorted-key UTF-8 JSON without a newline."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")

    def to_json(self) -> str:
        return self.canonical_bytes().decode("utf-8")

    def sha256(self) -> str:
        """Hash these declarations, not the checkpoint's actual contents."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def require_resolved_memory_writes(self) -> None:
        """Reject an unresolved live-episode prerequisite.

        Passing this check is not launch authorization or proof of enforcement.
        Metadata-only development sessions need not call it.
        """
        if self.memory_writes == "unresolved":
            raise EpisodeSpecError("memory_writes must be resolved before a live episode")

    @classmethod
    def from_dict(cls, value: Any) -> EpisodeSpec:
        """Read a complete recorded declaration without filling missing fields."""
        if type(value) is not dict:
            raise EpisodeSpecError("Episode specification must be a JSON object")
        if value.keys() != _FIELDS:
            raise EpisodeSpecError("Episode specification has missing or unknown fields")
        for name, expected in _FIXED_FIELDS.items():
            if type(value[name]) is not type(expected) or value[name] != expected:
                raise EpisodeSpecError(f"Unsupported {name}")
        return cls(**{name: value[name] for name in _FIELDS - _FIXED_FIELDS.keys()})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EpisodeSpecError("Duplicate JSON field in episode specification")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise EpisodeSpecError("Non-finite numbers are not valid episode specification JSON")


def load_spec(data: str | bytes) -> EpisodeSpec:
    """Parse bounded, strict UTF-8 JSON; preserve the objective's exact text."""
    if type(data) is str:
        encoded = _utf8(data, MAX_SPEC_BYTES, "Episode specification")
    elif type(data) is bytes:
        encoded = data
        if len(encoded) > MAX_SPEC_BYTES:
            raise EpisodeSpecError("Episode specification exceeds its byte limit")
    else:
        raise EpisodeSpecError("Episode specification input must be str or bytes")
    try:
        text = encoded.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, EpisodeSpecError):
            raise
        raise EpisodeSpecError("Invalid episode specification JSON") from exc
    return EpisodeSpec.from_dict(value)


def read_spec(path: str | Path) -> EpisodeSpec:
    """Read only the selected regular JSON file, with a finite read bound."""
    source = Path(path)
    try:
        if not stat.S_ISREG(source.stat().st_mode):
            raise EpisodeSpecError("Episode specification must be a regular file")
        with source.open("rb") as stream:
            data = stream.read(MAX_SPEC_BYTES + 1)
    except OSError as exc:
        raise EpisodeSpecError("Cannot read episode specification file") from exc
    return load_spec(data)
