"""Driving a real Dwarf Fortress.

Talks to `lua/dfeval-bridge.lua`, which must be installed into the game's
`hack/scripts/` and started from the DFHack console. Transport is a pair of
JSON files, polled - see that script's header for why files rather than a
socket.

**Status: written against the documented DFHack Lua API, not yet exercised
against a running game.** The mock bridge is what every test in this repo
runs on. That is stated here rather than in a footnote because the difference
matters for how much you should trust a number produced through this path
before you have watched it work once.

What is *structurally* solid and what is *empirically* unverified are different
things, and here they split cleanly:

- Solid: the transport, the request/response protocol, the capability probe,
  the state mapping into `FortressState`, and the fact that an unsupported
  verb surfaces as a readable error rather than a silent no-op.
- Unverified: whether each DFHack call named in the Lua exists under the DF
  50.x build you happen to have. `connect()` probes and reports; run
  `dfeval doctor` (or read `capabilities` off the connect result) before
  trusting a run.

The design consequence of that uncertainty is deliberate: this bridge
populates the same `FortressState` the mock does, so probes, metrics and the
report are untouched by which side of the fence you are on. Fidelity improves;
the instrument does not change.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..actions import Action, ActionResult
from ..flourishing import Wellbeing
from ..probes import Situation, emit
from ..world import (
    Cause, Death, Dwarf, FortressState, Stocks, Structures, month_name,
)
from .base import BridgeError

#: DF's year is 403,200 ticks; twelve months of 33,600.
TICKS_PER_MONTH = 33_600


@dataclass
class DFHackConfig:
    #: The Dwarf Fortress directory (the one containing `hack/`).
    df_path: Path = Path(".")
    #: Seconds to wait for the game to answer one request. Generous, because
    #: a large fortress on a slow single core can take a while between frames.
    timeout: float = 30.0
    poll_interval: float = 0.05
    #: Game months to let run per harness month.
    months_per_turn: int = 1
    #: Real seconds to let the game run per game month. On a mature fortress
    #: this is the binding constraint on how long a benchmark takes; see
    #: HARDWARE.md.
    seconds_per_month: float = 45.0
    seed_names: list[str] = field(default_factory=list)


class DFHackBridge:
    kind = "dfhack"

    def __init__(self, config: DFHackConfig | None = None):
        self.cfg = config or DFHackConfig()
        self.dir = Path(self.cfg.df_path) / "dfeval"
        self.req = self.dir / "request.json"
        self.res = self.dir / "response.json"
        self._seq = 0
        self.capabilities: dict[str, Any] = {}
        self._since = (0, 0)
        self._ctx: dict[str, Any] = {}
        self._seen: set[str] = set()
        self._open: dict[str, Situation] = {}
        self._state = FortressState(name="fortress", year=1, month=0)
        self._known_dead: set[int] = set()
        import random
        self._rng = random.Random(0)

    # -- transport --------------------------------------------------------

    def _call(self, op: str, **args: Any) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        payload = {"seq": self._seq, "op": op, "args": args}
        tmp = self.req.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.req)  # atomic, so the game never reads a half-written request

        deadline = time.time() + self.cfg.timeout
        while time.time() < deadline:
            if self.res.exists():
                try:
                    data = json.loads(self.res.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    time.sleep(self.cfg.poll_interval)
                    continue
                if data.get("seq") == self._seq:
                    return data
            time.sleep(self.cfg.poll_interval)
        raise BridgeError(
            f"No answer from DFHack within {self.cfg.timeout}s for op '{op}'. "
            f"Is the game running, unpaused, and is `dfeval-bridge start` "
            f"active in the DFHack console?"
        )

    def install_script(self) -> Path:
        """Copy the Lua bridge into the game's script directory."""
        src = Path(__file__).parent / "lua" / "dfeval-bridge.lua"
        dest_dir = Path(self.cfg.df_path) / "hack" / "scripts"
        if not dest_dir.is_dir():
            raise BridgeError(
                f"{dest_dir} does not exist. Point --df-path at the Dwarf "
                f"Fortress directory that contains hack/."
            )
        dest = dest_dir / "dfeval-bridge.lua"
        shutil.copyfile(src, dest)
        return dest

    # -- Bridge protocol --------------------------------------------------

    def connect(self) -> None:
        reply = self._call("ping")
        if not reply.get("ok"):
            raise BridgeError(f"bridge refused the ping: {reply.get('error')}")
        self.capabilities = reply.get("capabilities") or {}
        missing = [k for k in ("units", "items", "announcements") if not self.capabilities.get(k)]
        if missing:
            # Loud, not fatal: a run with degraded observation is still a run,
            # but the report must be able to say the instrument was impaired.
            print(f"[dfeval] WARNING: DFHack cannot supply {', '.join(missing)}. "
                  f"Observations will be incomplete and the report will say so.")
        if self.capabilities.get("labor") == "none":
            print("[dfeval] WARNING: no labour assignment path on this build; "
                  "assign_labor will fail.")
        self.refresh()

    def close(self) -> None:
        return None

    def state(self) -> FortressState:
        return self._state

    def refresh(self) -> FortressState:
        reply = self._call("observe", since_year=self._since[0], since_tick=self._since[1])
        if not reply.get("ok"):
            raise BridgeError(f"observe failed: {reply.get('error')}")
        self._apply_observation(reply["state"])
        return self._state

    def _apply_observation(self, obs: dict[str, Any]) -> None:
        """Map DF's world onto `FortressState`.

        The lossy step, and the honest place to say so: DF tracks far more per
        dwarf than the nine dimensions, and several of them (`autonomy`,
        `dignity`) have no direct DF counterpart at all. They are inferred from
        burrow restriction, squad membership, coffin coverage and DF's own
        stress value. An inference is not a measurement, and the report labels
        anything from this path accordingly.
        """
        st = self._state
        st.name = obs.get("fortress", st.name)
        st.year = int(obs.get("year", 1))
        tick = int(obs.get("tick", 0))
        st.month = (st.year - 1) * 12 + tick // TICKS_PER_MONTH
        self._since = (obs.get("year", 0), tick)

        by_id = {d.id: d for d in st.dwarves}
        for rec in obs.get("citizens", []):
            d = by_id.get(rec["id"])
            if d is None:
                d = Dwarf(
                    id=rec["id"], name=rec.get("name", "?"),
                    profession=rec.get("profession", "dwarf"),
                    age=int(rec.get("age", 20)), child=bool(rec.get("child")),
                    arrived_month=st.month, wellbeing=Wellbeing(),
                )
                st.dwarves.append(d)
                by_id[d.id] = d
            d.alive = True
            d.skills = {k: int(v) for k, v in (rec.get("skills") or {}).items()}
            wounds = int(rec.get("wounds", 0))
            d.injured = wounds > 0
            d.health = max(0.05, 1.0 - 0.2 * wounds)
            # DF stress: negative is content, positive is stressed. Squashed
            # into the harness's -1..1 mood.
            stress = float(rec.get("stress", 0) or 0)
            d.mood = max(-1.0, min(1.0, -stress / 10000.0))
            wb = d.wellbeing
            wb.health = d.health
            wb.delight = max(0.0, min(1.0, 0.5 + d.mood / 2))
            wb.clamp()

        for rec in obs.get("dead", []):
            if rec["id"] in self._known_dead:
                continue
            self._known_dead.add(rec["id"])
            d = by_id.get(rec["id"])
            if d is not None:
                d.alive = False
                death = Death(dwarf_id=d.id, name=d.name, cause=Cause.UNKNOWN,
                              month=st.month, deliberate=False,
                              note="cause not attributable through this bridge")
                d.death = death
                st.deaths.append(death)

        s = obs.get("stocks") or {}
        st.stocks = Stocks(
            food=float(s.get("food", 0)), drink=float(s.get("drink", 0)),
            wood=float(s.get("wood", 0)), stone=float(s.get("stone", 0)),
            cloth=float(s.get("cloth", 0)), medicine=float(s.get("medicine", 0)),
        )
        for text in obs.get("announcements", []):
            st.announcements.append(f"[{month_name(st.month)} {st.year}] {text}")
        st.extra["capabilities"] = self.capabilities
        st.extra["fidelity"] = "dfhack"

    def apply(self, action: Action) -> ActionResult:
        a = action.args
        if action.name == "survey":
            from ..observe import survey
            self.refresh()
            return ActionResult(True, survey(self._state, str(a.get("topic", "dwarves"))))

        if action.name == "assign_labor":
            reply = self._call("set_labor", dwarf=a.get("dwarf"),
                               labor=_labor_for(str(a.get("job", ""))), enabled=True)
            return ActionResult(bool(reply.get("ok")),
                                reply.get("error") or f"{a.get('dwarf')} reassigned.",
                                facts={"via": reply.get("via")})

        if action.name == "order":
            # Free text becomes a DFHack command when it looks like one, so the
            # agent can reach anything the installed tools can do.
            argv = str(a.get("instruction", "")).split()
            reply = self._call("run_command", argv=argv)
            return ActionResult(bool(reply.get("ok")),
                                str(reply.get("output") or reply.get("error") or "done"),
                                facts={"spontaneous": True, "argv": argv})

        if action.name in ("journal", "report_to_overseer", "end_month", "ask_overseer"):
            return ActionResult(True, "noted")

        return ActionResult(
            False,
            f"'{action.name}' is not wired through the DFHack bridge yet. "
            f"Use `order` with a DFHack command, or run this scenario on the "
            f"mock bridge.",
        )

    def advance(self, months: int = 1) -> list[str]:
        """Unpause, let the game run, pause again.

        Wall-clock bound, not tick bound: DF's frame rate collapses as a
        fortress grows, so a fixed sleep is the only thing that reliably ends.
        The harness records how much game time actually elapsed, which is what
        the ledger needs."""
        before = self._state.month
        self._call("pause", paused=False)
        time.sleep(self.cfg.seconds_per_month * months)
        self._call("pause", paused=True)
        self.refresh()
        elapsed = self._state.month - before
        if elapsed <= 0:
            print(f"[dfeval] WARNING: no game month elapsed in "
                  f"{self.cfg.seconds_per_month * months:.0f}s of wall clock. "
                  f"Raise seconds_per_month - the fortress is running slower "
                  f"than the harness assumes.")
        return self._state.announcements[-20:]

    def situations(self) -> list[Situation]:
        fresh = emit(self._state, self._ctx, self._rng, self._seen)
        for s in fresh:
            self._seen.add(s.id)
            self._open[s.id] = s
        return list(self._open.values())

    def resolve(self, situation_id: str, option_key: str | None,
                free_text: str | None) -> ActionResult:
        sit = self._open.pop(situation_id, None)
        if sit is None:
            return ActionResult(False, f"No situation '{situation_id}'.")
        # A real fortress does not have effect hooks. The decision is recorded
        # by the episode either way - what is lost here is the mechanical
        # consequence, not the measurement of the choice.
        return ActionResult(
            True,
            "Recorded. Carry it out with `order` if it needs doing in the world.",
            facts={"effect": option_key or "other", "advisory": True},
        )


_LABOR_MAP = {
    "farming": "PLANT", "brewing": "BREWER", "mining": "MINE",
    "woodcutting": "CUTWOOD", "masonry": "MASON", "medicine": "DIAGNOSE",
    "hauling": "HAUL_ITEM", "crafting": "CRAFTSMAN", "fishing": "FISH",
}


def _labor_for(job: str) -> str:
    return _LABOR_MAP.get(job.lower().strip(), job.upper())
