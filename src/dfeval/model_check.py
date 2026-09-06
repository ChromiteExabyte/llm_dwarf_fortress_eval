"""One explicit model connection check, without native game access.

This uses the same policy transport and decision validation as an experiment.
The observation is synthetic, every native measurement is unknown, and returned
actions are never dispatched. A successful check is not a gameplay evaluation.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
from pathlib import Path
import threading
import time
from typing import Any

from .policies import ChatCompletionsPolicy, Policy, PolicyError, json_bytes, validate_decision
from .model_observation import project_observation


_CONFIG_FIELDS = frozenset({
    "kind", "is_model", "mode", "endpoint", "model", "api_key_env",
    "max_completion_tokens", "token_limit_field", "response_format", "timeout", "max_response_bytes",
    "max_request_bytes", "system_prompt", "options", "keep_alive", "think", "model_identity",
    "base_url", "num_ctx", "temperature", "seed",
    "model_observation_version",
})
_EXCHANGE_FIELDS = frozenset({
    "request", "response_text", "response_bytes", "response_truncated", "usage",
    "reserved_output_tokens", "telemetry",
})


class _CheckTimeout(PolicyError):
    pass


def _choose_with_deadline(policy: Policy, observation: dict[str, Any], timeout: float) -> Any:
    """Discard a late result even if an endpoint keeps its socket read alive."""
    done = threading.Event()
    result, failure = [], []

    def invoke() -> None:
        try:
            result.append(policy.choose(observation, []))
        except BaseException as exc:
            failure.append(exc)
        finally:
            done.set()

    deadline = time.monotonic() + timeout
    threading.Thread(target=invoke, daemon=True, name="dfeval-model-check").start()
    if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() > deadline:
        raise _CheckTimeout("Model connection check exceeded its wall-time allowance; any late response is discarded.")
    if failure:
        raise failure[0]
    return result[0]


def _observation() -> dict[str, Any]:
    return {
        "origin": "synthetic_model_connection_check",
        "preflight": {
            "synthetic": True,
            "description": "SYNTHETIC MODEL CONNECTION CHECK. No game or fortress was inspected. "
                           "All native measurements are unknown. A returned decision is validated "
                           "as data only; no action will be executed. This is not a benchmark run.",
        },
        "df_version": None, "dfhack_version": None, "save_directory": None,
        "world_loaded": None, "map_loaded": None, "fortress_mode": None, "paused": None,
        "absolute_tick": None, "citizens": None, "known_former_citizens": None,
        "stocks": None, "workshops": None, "errors": None,
    }


def _public_exchange(policy: Policy) -> dict[str, Any] | None:
    exchange = getattr(policy, "last_exchange", None)
    if exchange is None:
        return None
    if not isinstance(exchange, dict):
        raise ValueError("Public model exchange must be an object or None")
    # The existing client exposes bodies and cleaned usage, never HTTP headers.
    # Ignore unrelated attributes rather than serializing a transport object.
    public = copy.deepcopy({key: value for key, value in exchange.items() if key in _EXCHANGE_FIELDS})
    json_bytes(public)
    return public


def _error(exc: BaseException, policy: Policy) -> dict[str, str]:
    if isinstance(exc, KeyboardInterrupt):
        message = "Model connection check interrupted; no action was executed."
    elif isinstance(exc, _CheckTimeout):
        message = str(exc)
    elif type(policy) is ChatCompletionsPolicy and isinstance(exc, PolicyError):
        # Built-in transport errors intentionally omit response headers, HTTP
        # error bodies and credentials. Unknown injected policy errors may not.
        message = str(exc)[:2000]
    else:
        message = "Model connection check failed; no action was executed. " \
                  "Unexpected exception details were omitted from the public report."
    return {"type": type(exc).__name__, "message": message}


def check_model(policy: Policy, *, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Call an explicitly named model once and return its public check report.

    ``output_dir=None`` writes nothing. A supplied new or empty directory gets
    ``model-check.json``, including failures after the call was attempted. Invalid
    policy configuration or an unusable output directory raises before any call.
    Connection/response failures return ``ok=False``; there is no retry, fallback,
    game launch, bridge operation, action execution, or native benchmark record.

    A caller supplies a configured policy, normally ChatCompletionsPolicy. Its
    configured timeout, response bytes and token limits remain in force. Cloud
    authentication checks belong to that existing transport and precede network
    access. No provider is contacted merely by importing this module.

    A daemon worker enforces the policy's timeout as an overall call deadline
    (30 seconds for injected policies without a declared timeout). A timed-out
    provider request can finish later; its decision is discarded. This function
    cannot guarantee provider-side cancellation or cancellation of billing.
    """
    if not callable(getattr(policy, "choose", None)) or not callable(getattr(policy, "public_config", None)):
        raise TypeError("policy must implement choose and public_config")
    config = policy.public_config()
    if not isinstance(config, dict) or config.get("is_model") is not True:
        raise ValueError("A model connection check requires an explicit model policy, not a baseline")
    if not isinstance(config.get("model"), str) or not config["model"].strip():
        raise ValueError("A model connection check requires an explicit model name")
    public_config = copy.deepcopy({key: value for key, value in config.items() if key in _CONFIG_FIELDS})
    json_bytes(public_config)
    timeout = public_config.get("timeout", 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("Model connection check timeout must be finite, positive and at most 3600 seconds")
    output = None
    if output_dir is not None:
        requested = Path(output_dir).expanduser()
        if requested.is_symlink():
            raise ValueError("output_dir must not be a symbolic link")
        output = requested.resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("output_dir must be new or empty")
        output.mkdir(parents=True, exist_ok=True)
    observation = project_observation(_observation())
    report: dict[str, Any] = {
        "schema_version": 1, "kind": "model_connection_check", "ok": False,
        "synthetic": True, "game_access": False, "actions_executed": 0,
        "at": datetime.now(timezone.utc).isoformat(), "wall_seconds": None,
        "policy": public_config, "observation": observation, "history": [],
        "decision": None, "publicexchange": None, "usage": None, "error": None,
        "policy_calls": 0, "output_directory": str(output) if output else None,
        "wall_timeout_seconds": timeout, "background_request_may_continue": False,
        "notes": [
            "Synthetic connection and decision-schema check; not a native game observation or benchmark result.",
            "Success means this policy call returned one valid decision. It does not measure care or game-playing ability.",
            "The public exchange contains the configured request body and bounded provider response, not hidden reasoning or HTTP headers.",
            "Any returned wait, brew or finish action is inspected as data only; no action is executed.",
        ],
    }
    started = time.monotonic()
    try:
        if hasattr(policy, "last_exchange"):
            policy.last_exchange = None
        report["policy_calls"] = 1
        decision = _choose_with_deadline(policy, copy.deepcopy(observation), timeout)
        report["decision"] = validate_decision(decision)
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = _error(exc, policy)
        report["background_request_may_continue"] = isinstance(exc, (_CheckTimeout, KeyboardInterrupt))
        if report["background_request_may_continue"]:
            report["notes"].append("A request already in progress may finish at the provider after this check stops; its late decision is discarded. Provider cancellation and billing cancellation are not guaranteed.")
    try:
        report["publicexchange"] = _public_exchange(policy)
        if report["publicexchange"] is not None:
            report["usage"] = report["publicexchange"].get("usage")
    except (Exception, KeyboardInterrupt) as exc:
        report["ok"] = False
        report["error"] = report["error"] or _error(exc, policy)
    report["wall_seconds"] = round(time.monotonic() - started, 6)
    if output is not None:
        # Exclusive creation prevents an unrelated concurrent writer's file
        # from being overwritten after directory validation.
        with (output / "model-check.json").open("xb") as stream:
            stream.write(json_bytes(report) + b"\n")
    return report
