"""Bounded, inspectable real-game brewing experiments.

This host runner owns the filesystem, model connection and bridge. A policy sees
only JSON observations and public decision history and returns one fixed-schema
decision. This is a narrow action interface, not an OS security sandbox. There is
no automatic mock, provider fallback, retry, save reset or game launch.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import time
from typing import Any, Callable
import uuid

from .care import summarize_events
from .comparison import initial_observation_fingerprint, validate_initial_expectation
from .live import LiveBridge, LiveBridgeError, PROTOCOL_VERSION
from .model_observation import (MODEL_OBSERVATION_VERSION, ModelObservationTooLarge,
                                model_input_bytes, project_observation, projection_contract)
from .policies import Policy, PolicyError, json_bytes, validate_decision


@dataclass(frozen=True)
class ExperimentConfig:
    max_decisions: int = 12
    ticks_per_decision: int = 1200
    max_total_ticks: int = 14400
    max_policy_calls: int = 12
    max_bridge_calls: int = 64
    max_output_tokens: int = 8192
    max_output_bytes: int = 262144
    max_wall_seconds: float = 600
    request_timeout: float = 30
    max_input_bytes: int = 2_000_000
    max_snapshot_bytes: int = 4_000_000
    max_log_bytes: int = 64_000_000
    history_decisions: int = 8
    stop_file: str | None = None
    starting_save_sha256: str | None = None
    simulation_fps: int | None = None

    def __post_init__(self) -> None:
        if self.simulation_fps is not None and (type(self.simulation_fps) is not int or not 1 <= self.simulation_fps <= 10000):
            raise ValueError("simulation_fps must be an integer from 1 to 10000, or None to preserve the current cap")
        for name, low, high in (
            ("max_decisions", 1, 10000), ("ticks_per_decision", 1, 12000),
            ("max_total_ticks", 0, 120_000_000), ("max_policy_calls", 1, 10000),
            ("max_bridge_calls", 4, 100000), ("max_output_tokens", 1, 100_000_000),
            ("max_output_bytes", 1, 100_000_000), ("max_input_bytes", 1024, 16_000_000),
            ("max_snapshot_bytes", 1024, 16_000_000), ("max_log_bytes", 1_000_000, 1_000_000_000),
            ("history_decisions", 0, 100),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        for name, high in (("max_wall_seconds", 86400), ("request_timeout", 3600)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= high:
                raise ValueError(f"{name} must be finite, positive and at most {high}")
        if self.stop_file is not None and (not isinstance(self.stop_file, str) or not self.stop_file):
            raise ValueError("stop_file must be a nonempty trusted-host path string or None")
        if self.starting_save_sha256 is not None and (not isinstance(self.starting_save_sha256, str) or
                not re.fullmatch(r"[0-9a-fA-F]{64}", self.starting_save_sha256)):
            raise ValueError("starting_save_sha256 must be 64 hexadecimal characters or None")


class _Stop(Exception):
    def __init__(self, outcome: str, detail: str):
        self.outcome, self.detail = outcome, detail
        super().__init__(detail)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json_bytes(value) + b"\n")
    temporary.replace(path)


def _bootstrap(bridge: LiveBridge, timeout: float) -> dict[str, Any]:
    """Start only the bundled bridge inside an already running DFHack game."""
    completed = subprocess.run(
        bridge.start_command(), cwd=bridge.game_dir, shell=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = {"returncode": completed.returncode, "stdout": completed.stdout[:8192],
              "stderr": completed.stderr[:8192],
              "output_truncated": len(completed.stdout) > 8192 or len(completed.stderr) > 8192}
    return result


def _policy_with_deadline(policy: Policy, observation: dict[str, Any], history: list[dict[str, Any]],
                          check: Callable[[], None]) -> dict[str, Any]:
    """Never dispatch a late decision, including during a slow HTTP response.

    Network activity belongs to a daemon worker: Python cannot forcibly stop a
    provider that ignores its timeout. On host cancellation its eventual result
    is discarded; no game access exists in the built-in model policy. Provider
    cancellation/billing is not guaranteed by stopping this runner.
    """
    done = threading.Event()
    result: list[Any] = []
    failure: list[BaseException] = []
    def invoke() -> None:
        try:
            result.append(policy.choose(observation, history))
        except BaseException as exc:
            failure.append(exc)
        finally:
            done.set()
    threading.Thread(target=invoke, daemon=True, name="dfeval-policy").start()
    while not done.wait(0.05):
        check()
    check()
    if failure:
        raise failure[0]
    return result[0]


def run_experiment(game_dir: Path | str, policy: Policy, *, config: ExperimentConfig | None = None,
                   output_dir: Path | str | None = None,
                   initial_expectation: dict[str, Any] | None = None,
                   starting_snapshot_path: str | None = None,
                   bridge_factory: Callable[..., Any] | None = None,
                   bootstrap: Callable[[Any, float], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run on the current loaded fortress and return the recorded result object.

    Policy is explicit: IdlePolicy/RulePolicy are named non-LLM baselines; a
    ChatCompletionsPolicy requires the operator's model and local/cloud choice.
    A failed real bridge ends the run. Tests inject a fake bridge and bootstrap.
    An elapsed tick count different from the requested count is an error; its
    resulting native snapshot is retained before stopping for inspection.
    All game calls are sequential. Final pause and optional FPS restoration each
    get up to request_timeout seconds of cleanup grace and a reserved call.
    A trusted human can create output_dir/STOP (or config.stop_file) to stop.
    STOP is polled between host operations and every 50 ms while awaiting the
    policy. It does not interrupt a blocking bootstrap or bridge call; those have
    request_timeout limits. Ctrl+C interrupts a bridge wait and publishes a
    cancellation for its active sequence before the final pause is requested.
    An optional initial_expectation guards the measured native starting state
    after pause/FPS setup, before the first policy call or gameplay action. It
    does not restore a save or establish equality of unobserved simulation state.
    """
    config = config or ExperimentConfig()
    if not isinstance(config, ExperimentConfig):
        raise TypeError("config must be ExperimentConfig")
    expectation = validate_initial_expectation(initial_expectation) if initial_expectation is not None else None
    if starting_snapshot_path is not None and (
            not isinstance(starting_snapshot_path, str) or not starting_snapshot_path.strip()
            or len(starting_snapshot_path) > 4096
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in starting_snapshot_path)):
        raise ValueError("starting_snapshot_path must be a bounded nonempty path reference")
    if not callable(getattr(policy, "choose", None)) or not callable(getattr(policy, "public_config", None)):
        raise TypeError("policy must implement choose and public_config")
    public_policy = copy.deepcopy(policy.public_config())
    json_bytes(public_policy)
    if not isinstance(public_policy, dict) or type(public_policy.get("is_model")) is not bool:
        raise ValueError("public policy config must identify is_model explicitly")
    is_model = public_policy["is_model"]
    if is_model and (not isinstance(public_policy.get("model"), str) or not public_policy["model"].strip()):
        raise ValueError("Model policies must name their model explicitly")
    if is_model and not callable(getattr(policy, "set_call_limits", None)):
        raise ValueError("Model policies must implement set_call_limits for bounded requests")
    if public_policy.get("model_observation_version", MODEL_OBSERVATION_VERSION) != MODEL_OBSERVATION_VERSION:
        raise ValueError("Policy declares an incompatible model observation version")
    public_policy.setdefault("model_observation_version", MODEL_OBSERVATION_VERSION)
    root = Path(game_dir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("game_dir must be an existing game installation directory")
    out = Path(output_dir) if output_dir is not None else Path("runs") / (
        datetime.now(timezone.utc).strftime("experiment-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    out = out.expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("output_dir must be new or empty")
    out.mkdir(parents=True, exist_ok=True)
    stop_path = Path(config.stop_file).expanduser().resolve() if config.stop_file else out / "STOP"
    public_config = asdict(config)
    public_config["stop_file"] = str(stop_path)
    public_config["policy"] = public_policy
    public_config["scenario"] = "drink-maintenance-v1"
    public_config["model_observation_version"] = MODEL_OBSERVATION_VERSION
    started = time.monotonic()
    deadline = started + config.max_wall_seconds
    manifest = {"schema_version": 1, "created_at": _utc(), "protocol": PROTOCOL_VERSION,
                "host": {"system": platform.system(), "machine": platform.machine(),
                         "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                         "python": platform.python_version()},
                "config": public_config, "policy": public_policy, "game_directory_name": root.name,
                "model_observation_version": MODEL_OBSERVATION_VERSION,
                "model_observation": projection_contract(),
                "starting_save_sha256": config.starting_save_sha256,
                "starting_snapshot_path": starting_snapshot_path,
                "initial_expectation": expectation, "initial_state_check": None,
                "starting_save_note": "Operator-declared restored-save identity; does not prove unchanged in-memory state",
                "events_file": "events.jsonl", "result_file": "result.json",
                "state": "running", "interface": "native measurements + wait/brew/finish",
                "memory": "versioned projection of the current native snapshot and last history_decisions validated public decisions; full raw snapshots retained separately",
                "budget_semantics": "output tokens reserve requested limits; observed usage is separate; requested ticks reserve before dispatch"}
    _write_json(out / "manifest.json", manifest)
    snapshots: list[dict[str, Any]] = []
    # Keep only the evidence needed to link native products to queue receipts.
    # Snapshot entries share the existing immutable capture; no second full log
    # or provider transcript is accumulated in memory.
    summary_evidence: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    budgets: dict[str, Any] = {"decisions": 0, "policy_calls": 0, "bridge_calls": 0,
        "requested_ticks": 0, "reported_elapsed_ticks": 0,
        "advance_calls_with_unknown_elapsed": 0,
        "output_tokens_reserved": 0, "output_bytes": 0,
        "reported_prompt_tokens": 0, "reported_completion_tokens": 0,
        "calls_with_unknown_usage": 0}
    bridge = None
    bridge_started = False
    pause_confirmed = False
    cleanup_calls = 2 if config.simulation_fps is not None else 1
    speed_attempted = False
    speed_restored = None
    outcome, detail = "error", "Run did not complete"
    turn = 0
    error_record = None
    initial_state_check = None
    log_bytes = 0
    event_number = 0
    # Reserve space for terminal events so a log budget stop remains inspectable.
    terminal_reserve = 131072
    with (out / "events.jsonl").open("wb") as stream:
        def emit(kind: str, *, terminal: bool = False, **fields: Any) -> None:
            nonlocal event_number, log_bytes
            event = {"event": event_number, "at": _utc(), "wall_seconds": time.monotonic() - started,
                     "kind": kind, "turn": turn, **fields}
            encoded = json_bytes(event) + b"\n"
            limit = config.max_log_bytes if terminal else config.max_log_bytes - terminal_reserve
            if log_bytes + len(encoded) > limit:
                raise _Stop("budget_exhausted", "max_log_bytes")
            stream.write(encoded)
            stream.flush()
            log_bytes += len(encoded)
            event_number += 1
            if kind == "snapshot":
                summary_evidence.append(event)
            elif kind == "run_start" or (kind == "action_result" and fields.get("operation") == "queue_brew"):
                summary_evidence.append(copy.deepcopy(event))

        def check() -> None:
            if stop_path.exists():
                raise _Stop("cancelled", "Human stop file detected")
            if time.monotonic() >= deadline:
                raise _Stop("budget_exhausted", "max_wall_seconds")

        def allowance() -> float:
            check()
            return max(0.001, min(config.request_timeout, deadline - time.monotonic()))

        def bridge_call(operation: str, arguments: dict[str, Any] | None = None,
                        *, cleanup: bool = False) -> dict[str, Any]:
            nonlocal pause_confirmed
            args = arguments or {}
            if not cleanup:
                check()
                if budgets["bridge_calls"] >= config.max_bridge_calls - cleanup_calls:
                    raise _Stop("budget_exhausted", "max_bridge_calls (calls reserved for final cleanup)")
            elif budgets["bridge_calls"] >= config.max_bridge_calls:
                raise LiveBridgeError("No reserved cleanup call remains")
            budgets["bridge_calls"] += 1
            bridge.timeout = config.request_timeout if cleanup else allowance()
            call_started = time.monotonic()
            try:
                if operation in ("queue_brew", "set_simulation_fps"):
                    result = getattr(bridge, operation)(**args)
                elif operation == "advance_ticks":
                    pause_confirmed = False
                    result = bridge.advance_ticks(**args, timeout=bridge.timeout)
                else:
                    result = getattr(bridge, operation)()
            except Exception as exc:
                emit("action_result", terminal=cleanup, operation=operation, arguments=args,
                     duration_seconds=time.monotonic() - call_started,
                     result=None, error={"type": type(exc).__name__, "message": str(exc)[:2000]})
                raise
            if not isinstance(result, dict):
                raise LiveBridgeError("Bridge operation result must be an object")
            if operation in ("pause", "advance_ticks"):
                pause_confirmed = result.get("paused") is True
            # observe has its own single unmodified snapshot event, not a second
            # large duplicate action_result payload.
            if operation != "observe":
                emit("action_result", terminal=cleanup, operation=operation, arguments=args, result=result,
                     duration_seconds=time.monotonic() - call_started)
            return result

        def observe(*, expected_tick: int | None = None,
                    advance_result: dict[str, Any] | None = None) -> dict[str, Any]:
            snapshot = bridge_call("observe")
            if len(json_bytes(snapshot)) > config.max_snapshot_bytes:
                raise _Stop("budget_exhausted", "max_snapshot_bytes; snapshot was not presented to the policy")
            captured = copy.deepcopy(snapshot)
            emit("snapshot", snapshot=captured)
            snapshots.append(captured)
            if len(snapshots) == 1:
                manifest.update(initial_snapshot_sha256=hashlib.sha256(json_bytes(snapshot)).hexdigest(),
                                initial_observation_sha256=initial_observation_fingerprint(snapshot),
                                initial_tick=snapshot.get("absolute_tick"))
                _write_json(out / "manifest.json", manifest)
            if any(snapshot.get(key) is not True for key in ("world_loaded", "map_loaded", "fortress_mode")):
                raise LiveBridgeError("A loaded fortress is required; no mock fallback is available")
            if snapshot.get("paused") is not True:
                raise LiveBridgeError("Native observation does not confirm the game is paused")
            tick = snapshot.get("absolute_tick")
            if type(tick) is not int or tick < 0:
                raise LiveBridgeError("Native observation has no valid absolute tick")
            save = snapshot.get("save_directory")
            if not isinstance(save, str) or not save:
                raise LiveBridgeError("Native observation has no save identity")
            if save != snapshots[0].get("save_directory"):
                raise LiveBridgeError("Native save identity changed during the experiment")
            if expected_tick is not None and tick != expected_tick:
                raise LiveBridgeError(
                    f"Game clock continuity failed: expected snapshot tick {expected_tick}, received {tick}")
            if advance_result is not None:
                elapsed = advance_result.get("elapsed_ticks")
                if type(elapsed) is not int or elapsed != config.ticks_per_decision:
                    raise LiveBridgeError("The game did not report exactly the requested elapsed ticks")
                if (type(advance_result.get("start_absolute_tick")) is not int or
                        advance_result["start_absolute_tick"] != expected_tick - config.ticks_per_decision or
                        type(advance_result.get("absolute_tick")) is not int or
                        advance_result["absolute_tick"] != expected_tick):
                    raise LiveBridgeError("Advance tick boundaries do not match the surrounding native observations")
            citizens = snapshot.get("citizens")
            if isinstance(citizens, list):
                if any(not isinstance(citizen, dict) for citizen in citizens):
                    raise LiveBridgeError("Native citizen roster contains invalid records")
                # An unknown roster or unknown dead flag is never an extinction
                # signal. Also handle older recordings/adapters that retained
                # explicitly dead citizens in this otherwise living roster.
                if not citizens or all(citizen.get("dead") is True for citizen in citizens):
                    raise _Stop("no_citizens", "Native observation contains no living citizens")
            return snapshot

        try:
            emit("run_start", config=public_config, initial_expectation=expectation)
            check()
            from .environment import capture_environment
            manifest["game_environment"] = capture_environment(root)
            _write_json(out / "manifest.json", manifest)
            emit("game_environment", fingerprint_sha256=manifest["game_environment"]["fingerprint_sha256"])
            check()
            bridge = (bridge_factory or LiveBridge)(root, timeout=allowance())
            installed = Path(bridge.install_script())
            script_bytes = installed.read_bytes()
            (out / "bridge-script.lua").write_bytes(script_bytes)
            manifest.update(session=bridge.session, bridge_sha256=hashlib.sha256(script_bytes).hexdigest())
            _write_json(out / "manifest.json", manifest)
            # Startup can time out after the script has begun running, so a
            # final pause is attempted even when its acknowledgement is lost.
            bridge_started = True
            bootstrap_result = (bootstrap or _bootstrap)(bridge, allowance())
            emit("bootstrap", result=bootstrap_result)
            if not isinstance(bootstrap_result, dict) or bootstrap_result.get("returncode") != 0:
                raise LiveBridgeError("Could not start the resident DFHack bridge")
            status = bridge_call("status")
            manifest.update(df_version=status.get("df_version"), dfhack_version=status.get("dfhack_version"),
                            save_directory=status.get("save_directory"))
            _write_json(out / "manifest.json", manifest)
            if any(status.get(key) is not True for key in ("world_loaded", "map_loaded", "fortress_mode")):
                raise LiveBridgeError("Load a fortress in Dwarf Fortress before starting an experiment")
            paused = bridge_call("pause")
            if paused.get("paused") is not True:
                raise LiveBridgeError("The bridge did not confirm the initial pause")
            if config.simulation_fps is not None:
                speed_attempted = True
                speed = bridge_call("set_simulation_fps", {"fps": config.simulation_fps})
                if speed.get("simulation_fps", {}).get("effective") != config.simulation_fps:
                    raise LiveBridgeError("Requested simulation FPS cap was not confirmed")
            observation = observe()
            if expectation is not None:
                actual = {"initial_observation_sha256": initial_observation_fingerprint(observation),
                          "bridge_sha256": manifest["bridge_sha256"], "protocol": PROTOCOL_VERSION}
                if "game_environment_sha256" in expectation:
                    actual["game_environment_sha256"] = manifest["game_environment"]["fingerprint_sha256"]
                for key, maximum in (("save_directory", 256), ("df_version", 128), ("dfhack_version", 128)):
                    value = observation.get(key)
                    actual[key] = value if (isinstance(value, str) and value.strip() and len(value) <= maximum
                                           and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)) else None
                differences = [{"field": key, "expected": expectation[key], "actual": actual[key]}
                               for key in sorted(expectation) if expectation[key] != actual[key]]
                initial_state_check = {"matched": not differences, "expected": copy.deepcopy(expectation),
                                       "actual": actual, "differences": differences}
                manifest["initial_state_check"] = initial_state_check
                _write_json(out / "manifest.json", manifest)
                emit("initial_state_check", **initial_state_check)
                if differences:
                    raise LiveBridgeError("Initial native state does not match the reference: " +
                                          ", ".join(item["field"] for item in differences))
            for turn in range(1, config.max_decisions + 1):
                check()
                if budgets["policy_calls"] >= config.max_policy_calls:
                    raise _Stop("budget_exhausted", "max_policy_calls")
                seen_history = copy.deepcopy(history[-config.history_decisions:]) if config.history_decisions else []
                try:
                    seen = project_observation(observation)
                    input_bytes = model_input_bytes(seen, seen_history)
                except ModelObservationTooLarge as exc:
                    raise _Stop("budget_exhausted", str(exc)) from exc
                if len(input_bytes) > config.max_input_bytes:
                    raise _Stop("budget_exhausted", "max_input_bytes; input was not sent")
                remaining_bytes = config.max_output_bytes - budgets["output_bytes"]
                if remaining_bytes <= 0:
                    raise _Stop("budget_exhausted", "max_output_bytes")
                reserved = 0
                if is_model:
                    remaining_tokens = config.max_output_tokens - budgets["output_tokens_reserved"]
                    if remaining_tokens <= 0:
                        raise _Stop("budget_exhausted", "max_output_tokens")
                    cap = public_policy.get("max_completion_tokens")
                    if type(cap) is not int or not 1 <= cap <= 32768:
                        raise PolicyError("Model policy has no valid declared completion token limit")
                    reserved = min(cap, remaining_tokens)
                    policy.set_call_limits(output_tokens=reserved,
                                           response_bytes=min(remaining_bytes, 2_000_000), timeout=allowance())
                emit("policy_input", observation=seen, history=seen_history,
                     model_observation_version=MODEL_OBSERVATION_VERSION,
                     input_bytes=len(input_bytes), input_sha256=hashlib.sha256(input_bytes).hexdigest())
                budgets["policy_calls"] += 1
                budgets["output_tokens_reserved"] += reserved
                policy_deadline = min(deadline, time.monotonic() + config.request_timeout)
                def check_policy() -> None:
                    check()
                    if time.monotonic() >= policy_deadline:
                        raise _Stop("budget_exhausted", "policy request_timeout")
                policy_started = time.monotonic()
                policy_error = None
                try:
                    if hasattr(policy, "last_exchange"):
                        policy.last_exchange = None
                    raw_decision = _policy_with_deadline(policy, seen, seen_history, check_policy)
                except BaseException as exc:
                    policy_error = {"type": type(exc).__name__, "message": str(exc)[:2000]}
                    raise
                finally:
                    exchange = copy.deepcopy(getattr(policy, "last_exchange", None))
                    if is_model:
                        usage = exchange.get("usage") if isinstance(exchange, dict) else None
                        if not isinstance(usage, dict) or any(type(usage.get(k)) is not int for k in ("prompt_tokens", "completion_tokens")):
                            budgets["calls_with_unknown_usage"] += 1
                        for source, target in (("prompt_tokens", "reported_prompt_tokens"),
                                               ("completion_tokens", "reported_completion_tokens")):
                            value = usage.get(source) if isinstance(usage, dict) else None
                            if type(value) is int and value >= 0:
                                budgets[target] += value
                        if isinstance(exchange, dict):
                            size = exchange.get("response_bytes", 0)
                            if type(size) is int and size >= 0:
                                budgets["output_bytes"] += size
                            emit("policy_response", exchange=exchange,
                                 duration_seconds=time.monotonic() - policy_started, error=policy_error)
                check()
                if not is_model:
                    raw_bytes = json_bytes(raw_decision)
                    budgets["output_bytes"] += len(raw_bytes)
                    emit("policy_response", duration_seconds=time.monotonic() - policy_started, error=None, exchange={
                        "response_text": raw_bytes[:remaining_bytes].decode("utf-8", errors="replace"),
                        "response_bytes": min(len(raw_bytes), remaining_bytes),
                        "response_truncated": len(raw_bytes) > remaining_bytes, "usage": None})
                decision = validate_decision(raw_decision)
                if budgets["output_bytes"] > config.max_output_bytes:
                    raise _Stop("budget_exhausted", "max_output_bytes")
                emit("decision", decision=decision)
                budgets["decisions"] += 1
                history.append(copy.deepcopy(decision))
                if decision["action"] == "finish":
                    outcome, detail = "finished", "Policy requested finish"
                    break
                if budgets["requested_ticks"] + config.ticks_per_decision > config.max_total_ticks:
                    raise _Stop("budget_exhausted", "max_total_ticks")
                needed_calls = 3 if decision["action"] == "brew" else 2
                if budgets["bridge_calls"] + needed_calls > config.max_bridge_calls - cleanup_calls:
                    raise _Stop("budget_exhausted", "max_bridge_calls; action and following observation require more calls")
                if decision["action"] == "brew":
                    bridge_call("queue_brew", {"workshop_id": decision["workshop_id"], "quantity": decision["quantity"]})
                expected_start_tick = observation["absolute_tick"]
                expected_end_tick = expected_start_tick + config.ticks_per_decision
                budgets["requested_ticks"] += config.ticks_per_decision
                budgets["advance_calls_with_unknown_elapsed"] += 1
                advanced = bridge_call("advance_ticks", {"ticks": config.ticks_per_decision})
                elapsed = advanced.get("elapsed_ticks")
                if type(elapsed) is int and elapsed >= 0:
                    budgets["reported_elapsed_ticks"] += elapsed
                    budgets["advance_calls_with_unknown_elapsed"] -= 1
                if advanced.get("paused") is not True:
                    raise LiveBridgeError("Advancing did not confirm a pause")
                observation = observe(expected_tick=expected_end_tick, advance_result=advanced)
            else:
                outcome, detail = "budget_exhausted", "max_decisions"
        except _Stop as exc:
            outcome, detail = exc.outcome, exc.detail
        except KeyboardInterrupt:
            outcome, detail = "cancelled", "Keyboard interrupt"
        except Exception as exc:
            outcome, detail = "error", str(exc)[:2000]
            error_record = {"type": type(exc).__name__, "message": detail}
            emit("error", terminal=True, **error_record)
        finally:
            if bridge is not None and bridge_started:
                try:
                    bridge_call("pause", cleanup=True)
                    if not pause_confirmed:
                        raise LiveBridgeError("Final pause was not confirmed")
                except Exception as exc:
                    pause_confirmed = False
                    emit("cleanup_error", terminal=True, type=type(exc).__name__, message=str(exc)[:2000])
                    if outcome not in ("error", "cancelled"):
                        outcome, detail = "error", "Final pause failed; inspect the game"
                if speed_attempted:
                    try:
                        restored = bridge_call("restore_simulation_fps", cleanup=True)
                        speed_restored = restored.get("restored") is True
                        if not speed_restored:
                            raise LiveBridgeError("Original simulation FPS cap restoration was not confirmed")
                    except Exception as exc:
                        speed_restored = False
                        emit("cleanup_error", terminal=True, type=type(exc).__name__, message=str(exc)[:2000])
                        if outcome not in ("error", "cancelled"):
                            outcome, detail = "error", "Simulation FPS restoration failed"
            summary = summarize_events(summary_evidence)
            result = {"schema_version": 1, "outcome": outcome, "detail": detail,
                      "ok": outcome in ("finished", "budget_exhausted") and pause_confirmed and error_record is None and speed_restored is not False,
                      "summary": summary, "budgets": budgets, "pause_confirmed": pause_confirmed,
                      "wall_seconds": time.monotonic() - started, "output_dir": str(out),
                      "error": error_record, "is_model": is_model, "speed_restored": speed_restored,
                      "initial_expectation": expectation, "initial_state_check": initial_state_check}
            _write_json(out / "result.json", result)
            # Normally include the complete recomputable summary. An oversized
            # terminal summary keeps its full version in result.json.
            terminal_summary = summary if len(json_bytes(summary)) < 64000 else {
                "detail_file": "result.json", "note": "Summary exceeds terminal-event space reserve"}
            emit("run_end", terminal=True, outcome=outcome, detail=detail, summary=terminal_summary,
                 budgets=budgets, pause_confirmed=pause_confirmed, speed_restored=speed_restored,
                 initial_expectation=expectation, initial_state_check=initial_state_check)
            manifest.update(state="finished", outcome=outcome, finished_at=_utc(), events=event_number,
                            event_bytes=log_bytes)
            _write_json(out / "manifest.json", manifest)
    return result
