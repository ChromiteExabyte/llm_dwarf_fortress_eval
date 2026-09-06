"""Trusted-host bootstrap and evidence capture for a small real-game probe.

This runner is not an LLM agent and does not score a fortress. It records what
the native bridge returned, including missing measurements and failed actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from typing import Any, Callable
import uuid

from .live import LiveBridge, MAX_ADVANCE_TICKS, PROTOCOL_VERSION


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_file(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def _validate(advance_ticks: int, brew_workshop: int | None, brew_jobs: int, timeout: float) -> None:
    if type(advance_ticks) is not int or not 0 <= advance_ticks <= MAX_ADVANCE_TICKS:
        raise ValueError(f"advance_ticks must be an integer from 0 to {MAX_ADVANCE_TICKS}")
    if brew_workshop is not None and (type(brew_workshop) is not int or not 0 <= brew_workshop <= 2**31 - 1):
        raise ValueError("brew_workshop must be a nonnegative native workshop ID")
    if type(brew_jobs) is not int or not 1 <= brew_jobs <= 10:
        raise ValueError("brew_jobs must be an integer from 1 to 10")
    if brew_workshop is None and brew_jobs != 1:
        raise ValueError("brew_jobs requires an explicit brew_workshop")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("timeout must be a finite number greater than 0 and at most 3600 seconds")


def _output_directory(output_dir: str | Path | None) -> Path:
    if output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = Path("runs") / f"live-{stamp}-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=False)
    else:
        path = Path(output_dir).expanduser().resolve()
        if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
            raise ValueError(f"Output directory must be empty; refusing to overwrite: {path}")
        path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _fortress_loaded(snapshot: dict[str, Any] | None) -> bool | None:
    if not snapshot:
        return None
    fields = [snapshot.get(key) for key in ("world_loaded", "map_loaded", "fortress_mode")]
    if any(value is False for value in fields):
        return False
    return True if all(value is True for value in fields) else None


def _count_citizens(snapshot: dict[str, Any] | None) -> int | None:
    citizens = (snapshot or {}).get("citizens")
    return len(citizens) if isinstance(citizens, list) else None


def _drink_units(snapshot: dict[str, Any] | None) -> int | None:
    stocks = (snapshot or {}).get("stocks")
    if not isinstance(stocks, dict) or not isinstance(stocks.get("by_item_type"), dict):
        return None
    drink = stocks["by_item_type"].get("DRINK")
    value = drink.get("stack_units") if isinstance(drink, dict) else None
    return value if type(value) is int else None


def _text_output(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def run_probe(game_dir: str | Path, output_dir: str | Path | None = None,
              advance_ticks: int = 0, brew_workshop: int | None = None,
              brew_jobs: int = 1, timeout: float = 30) -> dict[str, Any]:
    """Bootstrap a bridge and record snapshots, optionally queueing/advancing.

    The default does not change pause state or issue gameplay orders. Positive
    ``advance_ticks`` or a supplied ``brew_workshop`` explicitly enable those
    actions, after confirming a loaded fortress and pausing it. Queued jobs
    are never reported as completed. Runtime failures return ``ok=False``;
    invalid arguments and nonempty output directories raise ``ValueError``
    before bootstrap. The caller must use ``ok`` as its process exit status.
    """
    _validate(advance_ticks, brew_workshop, brew_jobs, timeout)
    game_path = Path(game_dir).expanduser().resolve()
    if not game_path.is_dir():
        raise ValueError(f"Game directory does not exist: {game_path}")
    output = _output_directory(output_dir)
    started = time.monotonic()
    mutating = advance_ticks > 0 or brew_workshop is not None
    bridge = None
    status = before = after = advance = brewing = None
    mutation_started = False
    pause_confirmed = None
    failure = None
    secondary_errors: list[str] = []
    artifacts: dict[str, str] = {"events": str(output / "events.jsonl"), "session": str(output / "session.json")}
    metadata: dict[str, Any] = {
        "created_at": _utc_now(), "game_dir": str(game_path), "protocol": PROTOCOL_VERSION,
        "mode": "mutating_probe" if mutating else "snapshot", "session": None,
        "requested": {"advance_ticks": advance_ticks, "brew_workshop": brew_workshop,
                      "brew_jobs": brew_jobs if brew_workshop is not None else None, "timeout": timeout},
        "script_sha256": None,
    }
    _json_file(output / "session.json", metadata)
    with (output / "events.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        event_sequence = 0

        def event(kind: str, **fields: Any) -> None:
            nonlocal event_sequence
            event_sequence += 1
            value = {"event": event_sequence, "at": _utc_now(),
                     "wall_seconds": time.monotonic() - started, "kind": kind, **fields}
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

        def save(name: str, value: Any) -> None:
            path = output / f"{name}.json"
            _json_file(path, value)
            artifacts[name] = str(path)

        def call(operation: str, arguments: dict[str, Any], fn: Callable[[], Any],
                 *, label: str | None = None) -> Any:
            event("request", operation=operation, arguments=arguments, label=label)
            call_started = time.monotonic()
            try:
                value = fn()
            except Exception as exc:
                event("error", operation=operation, label=label, error=str(exc), error_type=type(exc).__name__,
                      duration_seconds=time.monotonic() - call_started)
                raise
            event("result", operation=operation, label=label, result=str(value) if isinstance(value, Path) else value,
                  duration_seconds=time.monotonic() - call_started)
            return value

        event("probe_start", **metadata)
        try:
            bridge = LiveBridge(game_path, timeout=timeout)
            metadata.update(session=bridge.session, ipc_dir=str(bridge.ipc_dir))
            script = call("install_script", {}, bridge.install_script)
            script_bytes = Path(script).read_bytes()
            metadata.update(script_path=str(script), script_sha256=hashlib.sha256(script_bytes).hexdigest())
            script_copy = output / "bridge-script.lua"
            script_copy.write_bytes(script_bytes)
            artifacts["script"] = str(script_copy)
            command = bridge.start_command()
            metadata["startup_command"] = command
            _json_file(output / "session.json", metadata)

            def start() -> dict[str, Any]:
                launch_started = time.monotonic()
                try:
                    process = subprocess.run(
                        command, cwd=game_path, timeout=timeout, capture_output=True,
                        text=True, encoding="utf-8", errors="replace", shell=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except subprocess.TimeoutExpired as exc:
                    launch = {"command": command, "cwd": str(game_path), "returncode": None,
                              "stdout": _text_output(exc.stdout), "stderr": _text_output(exc.stderr),
                              "timed_out": True, "wall_seconds": time.monotonic() - launch_started}
                    save("startup", launch)
                    raise RuntimeError(f"DFHack bridge startup timed out after {timeout:g}s") from exc
                launch = {"command": command, "cwd": str(game_path), "returncode": process.returncode,
                          "stdout": process.stdout, "stderr": process.stderr, "timed_out": False,
                          "wall_seconds": time.monotonic() - launch_started}
                save("startup", launch)
                if process.returncode != 0:
                    raise RuntimeError(f"DFHack bridge startup failed (exit {process.returncode}): {process.stderr.strip() or process.stdout.strip()}")
                return launch

            call("bootstrap", {"command": command, "cwd": str(game_path)}, start)
            status = call("status", {}, bridge.status)
            save("status", status)
            before = call("observe", {}, bridge.observe, label="before")
            save("before", before)
            if mutating:
                if _fortress_loaded(status) is not True or _fortress_loaded(before) is not True:
                    raise RuntimeError("A loaded fortress must be confirmed before queueing jobs or advancing time")
                mutation_started = True
                paused = call("pause", {}, bridge.pause)
                save("pause", paused)
                pause_confirmed = paused.get("paused") is True
                if not pause_confirmed:
                    raise RuntimeError("Pause was not confirmed; no brewing or advance request was sent")
                if brew_workshop is not None:
                    brewing = call("queue_brew", {"workshop_id": brew_workshop, "quantity": brew_jobs},
                                   lambda: bridge.queue_brew(brew_workshop, quantity=brew_jobs))
                    save("brew", brewing)
                if advance_ticks > 0:
                    # It may be running until this operation acknowledges a pause.
                    pause_confirmed = None
                    advance = call("advance_ticks", {"ticks": advance_ticks, "timeout": timeout},
                                   lambda: bridge.advance_ticks(advance_ticks, timeout=timeout))
                    save("advance", advance)
                    pause_confirmed = advance.get("paused") is True
                    if not pause_confirmed:
                        raise RuntimeError("Advance returned without confirming the game is paused")
            after = call("observe", {}, bridge.observe, label="after")
            save("after", after)
        except Exception as exc:
            failure = {"type": type(exc).__name__, "message": str(exc)}
            event("probe_error", **failure)
            if mutation_started and bridge is not None:
                pause_confirmed = None
                try:
                    recovered = call("pause", {}, bridge.pause, label="recovery")
                    save("recovery-pause", recovered)
                    pause_confirmed = recovered.get("paused") is True
                    if not pause_confirmed:
                        secondary_errors.append("Recovery request returned without confirming pause")
                except Exception as recovery_error:
                    secondary_errors.append(f"Recovery pause unconfirmed: {recovery_error}")
                if after is None:
                    try:
                        after = call("observe", {}, bridge.observe, label="after_error")
                        save("after", after)
                    except Exception as observation_error:
                        secondary_errors.append(f"Post-error observation unavailable: {observation_error}")
        finally:
            # Preserve session evidence even if installing or bootstrapping fails.
            _json_file(output / "session.json", metadata)

        last = after or before or status or {}
        first_tick = (before or {}).get("absolute_tick")
        last_tick = (after or {}).get("absolute_tick")
        same_world = before is not None and after is not None and before.get("save_directory") is not None and before.get("save_directory") == after.get("save_directory")
        observed_delta = last_tick - first_tick if same_world and type(first_tick) is int and type(last_tick) is int and last_tick >= first_tick else None
        summary = {
            "df_version": last.get("df_version"), "dfhack_version": last.get("dfhack_version"),
            "fortress_loaded": _fortress_loaded(last),
            "citizens_before": _count_citizens(before), "citizens_after": _count_citizens(after),
            "drink_stack_units_before": _drink_units(before), "drink_stack_units_after": _drink_units(after),
            "requested_advance_ticks": advance_ticks,
            "actual_advance_ticks": (advance or {}).get("elapsed_ticks"),
            "observed_tick_delta": observed_delta,
            "queued_brew_jobs": (brewing or {}).get("queued_jobs"),
            "queued_job_ids": (brewing or {}).get("job_ids"),
            "brewing_completion_verified": False,
            "pause_confirmed": pause_confirmed,
            "paused_in_last_snapshot": last.get("paused"),
        }
        artifacts["result"] = str(output / "result.json")
        result = {"ok": failure is None, "mode": metadata["mode"], "session": metadata["session"],
                  "output_dir": str(output), "summary": summary, "artifacts": artifacts,
                  "error": failure, "secondary_errors": secondary_errors,
                  "wall_seconds": time.monotonic() - started}
        event("probe_end", **result)
        save("result", result)
        return result


def format_summary(result: dict[str, Any]) -> str:
    """Describe native measurements without converting them into a score."""
    summary = result["summary"]

    def show(value: Any) -> str:
        if value is None:
            return "unknown"
        if type(value) is bool:
            return "yes" if value else "no"
        return str(value)

    lines = [
        f"Live {'snapshot' if result['mode'] == 'snapshot' else 'probe'}: {'completed' if result['ok'] else 'failed'}",
        f"Dwarf Fortress {show(summary['df_version'])}; DFHack {show(summary['dfhack_version'])}",
        f"Fortress loaded: {show(summary['fortress_loaded'])}",
        f"Living citizens: {show(summary['citizens_before'])} -> {show(summary['citizens_after'])}",
        f"DRINK stack units: {show(summary['drink_stack_units_before'])} -> {show(summary['drink_stack_units_after'])}",
    ]
    if summary["requested_advance_ticks"]:
        lines.append(f"Advance ticks: {summary['requested_advance_ticks']} requested; {show(summary['actual_advance_ticks'])} confirmed")
    else:
        lines.append("No simulation advance requested.")
    lines.append(f"Tick change between snapshots: {show(summary['observed_tick_delta'])}")
    if summary["queued_brew_jobs"] is not None:
        lines.append(f"Brewing jobs queued: {summary['queued_brew_jobs']}; completion not verified.")
    if result["mode"] != "snapshot":
        lines.append(f"Pause confirmed: {show(summary['pause_confirmed'])}")
    if result["error"]:
        lines.append(f"Error: {result['error']['message']}")
    lines.extend(f"Additional error: {error}" for error in result["secondary_errors"])
    lines.append(f"Evidence: {result['output_dir']}")
    return "\n".join(lines)
