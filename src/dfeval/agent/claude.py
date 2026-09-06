"""The Claude overseer.

A manual tool-use loop rather than the SDK's tool runner, for one reason that
is specific to evals: **the transcript is the instrument.** Every request and
every response has to land in the ledger verbatim and in order, because the
honesty metric compares what the model said to what the harness saw, and the
evaluation-awareness condition compares runs turn by turn. A loop that owned
its own control flow would be more convenient and would put a layer between
the model's output and the record of it.

Two other deliberate choices:

**Memory is the model's own journal.** Rather than the harness writing a
summary of the run so far - which would decide for the model what it
remembers, and cue it by what we chose to mention - the agent carries forward
its own journal entries plus a verbatim window of recent months. What it
writes down is what it will still know next year. What it leaves out of the
journal, it forgets. This makes memory an action with consequences instead of
harness furniture, and it means the journal is simultaneously the memory and
the evidence.

This experimental adapter does not request server-side model fallback.
Refusals are logged and end the month. The legacy `allow_fallbacks` config
field is unused. Paid API behavior and complete usage accounting have not
been validated; the cost estimate is incomplete, including reflection calls
and mixed cache lifetimes.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..actions import anthropic_tool_defs
from .base import ToolCaller

DEFAULT_MODEL = "claude-opus-5"


@dataclass
class ClaudeConfig:
    model: str = DEFAULT_MODEL
    effort: str = "high"           # low | medium | high | xhigh | max
    max_tokens: int = 16000
    #: Months of verbatim transcript carried forward, on top of the journal.
    window_months: int = 3
    #: Hard stop on tool calls per month, so a loop cannot burn a budget.
    max_calls_per_month: int = 25
    thinking_display: str = "summarized"
    allow_fallbacks: bool = False
    #: Set by the harness; used only for logging.
    on_event: Any = None


class ClaudeAgent:
    name = "claude"

    def __init__(self, config: ClaudeConfig | None = None):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "The Claude agent needs the Anthropic SDK. Install it with:\n"
                "    pip install anthropic\n"
                "or run a baseline agent instead (--agent custodian)."
            ) from exc
        import anthropic

        self.cfg = config or ClaudeConfig()
        self.client = anthropic.Anthropic()
        self._anthropic = anthropic
        self.tools = anthropic_tool_defs()
        self.system: list[dict[str, Any]] = []
        #: The agent's own words, in order. Its memory and its testimony.
        self.journal: list[tuple[int, str]] = []
        self._window: deque[list[dict[str, Any]]] = deque(maxlen=self.cfg.window_months)
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.refusals: list[dict[str, Any]] = []
        self.name = f"claude:{self.cfg.model}"

    # -- Agent protocol ---------------------------------------------------

    def begin(self, briefing: str) -> None:
        # Frozen for the run, so it sits at the head of the cache prefix and
        # stays byte-identical across every request. No clock, no counters, no
        # per-turn interpolation in here - those are the classic silent cache
        # invalidators and they would also leak state the agent should be
        # discovering rather than being told.
        self.system = [{
            "type": "text",
            "text": briefing,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]

    def turn(self, observation: str, call_tool: ToolCaller, month: int) -> None:
        messages = self._build_messages(observation)
        this_month: list[dict[str, Any]] = [messages[-1]]
        calls = 0

        while calls < self.cfg.max_calls_per_month:
            response = self._request(messages)
            if response is None:
                break

            if getattr(response, "stop_reason", None) == "refusal":
                details = getattr(response, "stop_details", None)
                rec = {
                    "month": month,
                    "category": getattr(details, "category", None),
                    "explanation": getattr(details, "explanation", None),
                }
                self.refusals.append(rec)
                self._emit("refusal", rec)
                break

            messages.append({"role": "assistant", "content": response.content})
            this_month.append({"role": "assistant", "content": response.content})

            said = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if said:
                self._emit("said", {"month": month, "text": said})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break  # it had nothing more to do; the harness ends the month

            results = []
            finished = False
            for tu in tool_uses:
                calls += 1
                # Tool inputs are parsed JSON already; never string-match on the
                # serialised form - escaping varies by model.
                args = dict(tu.input) if isinstance(tu.input, dict) else {}
                if tu.name == "journal":
                    self.journal.append((month, str(args.get("entry", ""))))
                out = call_tool(tu.name, args)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": out or "(no reply)",
                })
                if tu.name in ("end_month", "ask_overseer"):
                    finished = True

            msg = {"role": "user", "content": results}
            messages.append(msg)
            this_month.append(msg)
            if finished:
                break

        self._window.append(this_month)

    def reflect(self, question: str) -> str:
        """The exit interview, asked with no tools and no fortress to act on.

        Carries the journal so the agent answers with its own record in front
        of it - the same memory it had all run - rather than from whatever
        survived the context window. Tools are withheld deliberately: this is
        the one turn where there is nothing to do but say what it thought it
        was doing."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": self._journal_block()}]},
            {"role": "user", "content": question},
        ]
        try:
            with self.client.messages.stream(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                system=self.system,
                messages=messages,
                thinking={"type": "adaptive", "display": self.cfg.thinking_display},
                output_config={"effort": self.cfg.effort},
            ) as stream:
                response = stream.get_final_message()
        except self._anthropic.APIError as exc:
            self._emit("error", {"error": "reflect_failed", "detail": str(exc)})
            return ""
        if getattr(response, "stop_reason", None) == "refusal":
            self._emit("refusal", {"month": -1, "where": "reflect"})
            return ""
        return "\n".join(b.text for b in response.content if b.type == "text").strip()

    def finish(self) -> None:
        return None

    # -- internals --------------------------------------------------------

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if callable(self.cfg.on_event):
            self.cfg.on_event(kind, data)

    def _journal_block(self) -> str:
        if not self.journal:
            return "Your journal is empty. Nothing has been written down yet."
        lines = ["Your journal, in your own hand. This is what you still know:"]
        for m, entry in self.journal:
            lines.append(f"  [month {m}] {entry}")
        return "\n".join(lines)

    def _build_messages(self, observation: str) -> list[dict[str, Any]]:
        # The journal grows by append only, so within a single month every
        # request shares this exact prefix and reads from cache. Across months
        # it lengthens by a line or two, which is the intended and unavoidable
        # invalidation.
        head: dict[str, Any] = {
            "role": "user",
            "content": [{
                "type": "text",
                "text": self._journal_block(),
                "cache_control": {"type": "ephemeral"},
            }],
        }
        messages: list[dict[str, Any]] = [head]
        for month_block in self._window:
            messages.extend(month_block)
        messages.append({"role": "user", "content": observation})
        return messages

    def _request(self, messages: list[dict[str, Any]]):
        A = self._anthropic
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": self.system,
            "messages": messages,
            "tools": self.tools,
            "thinking": {"type": "adaptive", "display": self.cfg.thinking_display},
            "output_config": {"effort": self.cfg.effort},
        }
        try:
            # Streaming, because a long-horizon turn at high effort can run past
            # the non-streaming HTTP timeout.
            with self.client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
        except A.BadRequestError as exc:
            self._emit("error", {"error": "bad_request", "detail": str(exc.message)})
            raise
        except A.RateLimitError as exc:
            retry = exc.response.headers.get("retry-after", "60")
            self._emit("error", {"error": "rate_limit", "retry_after": retry})
            raise
        except A.APIStatusError as exc:
            self._emit("error", {"error": f"status_{exc.status_code}", "detail": str(exc)})
            raise
        except A.APIConnectionError as exc:
            self._emit("error", {"error": "connection", "detail": str(exc)})
            raise

        u = response.usage
        self.usage["input"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output"] += getattr(u, "output_tokens", 0) or 0
        self.usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        self._emit("usage", {
            "input": getattr(u, "input_tokens", 0),
            "output": getattr(u, "output_tokens", 0),
            "cache_read": getattr(u, "cache_read_input_tokens", 0),
            "cache_write": getattr(u, "cache_creation_input_tokens", 0),
            "request_id": getattr(response, "_request_id", None),
        })
        return response

    def journal_text(self) -> str:
        return "\n".join(f"[month {m}] {e}" for m, e in self.journal)

    def cost_estimate(self) -> dict[str, float]:
        """Rough dollars, at Opus 5 list prices. Reported so a run's cost is
        visible next to its result - a benchmark nobody can afford to run is
        not a benchmark."""
        if not self.cfg.model.startswith("claude-opus-5"):
            return {}
        inp, out = 5.0 / 1e6, 25.0 / 1e6
        return {
            "input_usd": self.usage["input"] * inp,
            "cache_write_usd": self.usage["cache_write"] * inp * 1.25,
            "cache_read_usd": self.usage["cache_read"] * inp * 0.1,
            "output_usd": self.usage["output"] * out,
            "total_usd": (
                self.usage["input"] * inp
                + self.usage["cache_write"] * inp * 1.25
                + self.usage["cache_read"] * inp * 0.1
                + self.usage["output"] * out
            ),
        }
