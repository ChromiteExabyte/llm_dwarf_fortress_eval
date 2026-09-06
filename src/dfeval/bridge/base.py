"""What a bridge is.

A bridge is anything that can hold a `FortressState`, accept an `Action`, and
let a month pass. Two exist:

- `MockBridge`  - a deterministic colony simulator. Runs anywhere, needs no
                  game, no GPU, no 2TB drive. Used for development, for CI, and
                  for the cheap high-N runs where you need a hundred seeds.
- `DFHackBridge` - a real Dwarf Fortress, driven through a resident DFHack Lua
                  script over JSON files. Slow, heavy, and the only one whose
                  situations are genuinely emergent.

The eval does not know which one it is talking to. That is the point: the
metrics read the ledger, the ledger is written from `FortressState`, and both
bridges produce the same shape. A result that only reproduces on the mock is a
result about the mock, and the report says so.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..actions import Action, ActionResult
from ..probes import Situation
from ..world import FortressState


@runtime_checkable
class Bridge(Protocol):
    #: Short name that ends up in the ledger and the report header.
    kind: str

    def connect(self) -> None:
        """Open the game, or set up the simulation. Idempotent."""

    def state(self) -> FortressState:
        """Current ground truth. Never filtered for the agent - the
        observation layer does that separately, so the harness always retains
        what the agent was not told."""

    def apply(self, action: Action) -> ActionResult:
        """Carry out one action. Must not advance time."""

    def advance(self, months: int = 1) -> list[str]:
        """Let time pass. Returns new announcements, in the game's voice."""

    def situations(self) -> list[Situation]:
        """Choice points currently open."""

    def resolve(self, situation_id: str, option_key: str | None,
                free_text: str | None) -> ActionResult:
        """Apply the chosen option. `option_key=None` with `free_text` set is
        the open-ended answer and should be interpreted as best the bridge
        can, marking the result `spontaneous`."""

    def close(self) -> None:
        ...


class BridgeError(RuntimeError):
    pass
