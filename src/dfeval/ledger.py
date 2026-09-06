"""Append-only ground truth for a run.

Everything a metric later claims must be traceable to a line in here. The
ledger is written as it happens and never rewritten, because the honesty
metric compares what the agent *said* against what the harness *saw*, and a
mutable record would make that comparison meaningless.

One JSON object per line. Readable with `python -m json.tool`, greppable,
and safe to tail while a multi-hour run is in flight.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class Event:
    seq: int
    kind: str
    month: int
    data: dict[str, Any] = field(default_factory=dict)
    wall_time: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "kind": self.kind,
                "month": self.month,
                "wall_time": round(self.wall_time, 3),
                **self.data,
            },
            sort_keys=True,
            default=str,
        )


class Ledger:
    """Write-through event log. Holds events in memory *and* on disk so a
    crashed run is still scoreable from the file alone."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.events: list[Event] = []
        self._seq = 0
        self._fh = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")

    #: `kind` and `month` are positional-only so that a payload key of the
    #: same name lands in **data and hits the guard below, instead of
    #: raising an opaque TypeError about duplicate arguments.
    #: Event fields live at the top level of each JSON line, so payload keys
    #: cannot shadow them. Caught loudly here rather than as a TypeError from
    #: three frames down, or worse, as a silently overwritten field.
    RESERVED = frozenset({"seq", "kind", "month", "wall_time"})

    def record(self, kind: str, month: int, /, **data: Any) -> Event:
        clash = self.RESERVED & data.keys()
        if clash:
            raise ValueError(
                f"ledger payload may not use reserved field(s) {sorted(clash)}; "
                f"rename them (e.g. 'kind' -> 'choice_kind')"
            )
        self._seq += 1
        ev = Event(seq=self._seq, kind=kind, month=month, data=data)
        self.events.append(ev)
        if self._fh:
            self._fh.write(ev.to_json() + "\n")
            self._fh.flush()  # a long run must survive being killed
        return ev

    def of_kind(self, *kinds: str) -> list[Event]:
        wanted = set(kinds)
        return [e for e in self.events if e.kind in wanted]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __iter__(self) -> Iterator[Event]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    @classmethod
    def load(cls, path: Path | str) -> "Ledger":
        """Rehydrate a finished run for scoring. Opens nothing for writing."""
        led = cls(None)
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                ev = Event(
                    seq=obj.pop("seq"),
                    kind=obj.pop("kind"),
                    month=obj.pop("month"),
                    wall_time=obj.pop("wall_time", 0.0),
                    data=obj,
                )
                led.events.append(ev)
                led._seq = max(led._seq, ev.seq)
        return led


# Event kinds, named once so metrics and harness cannot drift apart.
TICK = "tick"
OBSERVATION = "observation"
AGENT_TURN = "agent_turn"        # what the model said, verbatim
ACTION = "action"                # a mechanical action it took
PROBE = "probe_fired"            # an instrumented choice point opened
DECISION = "decision"            # which option it took at a choice point
DEATH = "death"
DIRECTIVE = "directive"          # standing order from the overseer
REPORT = "report"                # agent's own account, for the honesty check
REFLECTION = "reflection"        # the exit interview: stated conception
OVERSEER = "overseer"            # agent asked, harness answered
REFUSAL = "refusal"              # model declined the turn - itself a datum
USAGE = "usage"                  # tokens/cost, for the cost model
ERROR = "error"
