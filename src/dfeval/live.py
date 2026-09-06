"""Narrow, auditable transport to a resident DFHack script in the real game.

The trusted runner owns this transport, including setup and pacing controls.
The evaluated policy only receives the runner's validated wait/brew/finish action
contract, never dfhack-run, Lua, a shell, or simulation pacing operations.
This is an action boundary, not an operating-system security sandbox.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import platform
import shutil
import threading
import time
import uuid
from typing import Any

PROTOCOL_VERSION = 1
MAX_ADVANCE_TICKS = 12000
MAX_SIMULATION_FPS = 10000
# Covers the runner's largest permitted 16,000,000-byte native snapshot plus
# its protocol envelope. Enforce before decoding/allocating the JSON object.
MAX_RESPONSE_BYTES = 16_777_216
ALLOWED_OPERATIONS = frozenset({"status", "observe", "pause", "advance_ticks", "queue_brew",
                                "set_simulation_fps", "restore_simulation_fps"})


class LiveBridgeError(RuntimeError):
    """The real-game adapter rejected an operation or returned invalid data."""


class LiveBridgeTimeout(LiveBridgeError, TimeoutError):
    """The game did not answer before the client deadline.

    A cancellation file is written. A responsive server pauses an active advance
    on its next frame. An unresponsive process cannot acknowledge a pause.
    """


def resolve_dfhack_runner(game_dir: Path | str) -> Path:
    """Find a runner for this host without launching it or guessing compatibility.

    Windows release layouts put the executable under hack/ or at the game root;
    Linux releases normally put the executable at the root. Paths are returned
    as Paths for subprocess argument lists, so spaces and Unicode need no shell
    quoting. A file's presence does not establish that its binary is compatible.
    """
    root = Path(game_dir).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        candidates = ("hack/dfhack-run.exe", "dfhack-run.exe")
    elif system == "Linux":
        candidates = ("dfhack-run", "hack/dfhack-run")
    else:
        raise LiveBridgeError(
            f"Live bridge startup does not support the {system or 'unknown'} host platform. "
            "Use native Windows or Linux with a matching Dwarf Fortress and DFHack installation.")
    nonexecutable = []
    for relative in candidates:
        candidate = root / relative
        if not candidate.is_file():
            continue
        if system == "Linux" and not os.access(candidate, os.X_OK):
            nonexecutable.append(str(candidate))
            continue
        return candidate
    if nonexecutable:
        raise LiveBridgeError(
            "DFHack runner exists but is not executable: " + ", ".join(nonexecutable) +
            ". Set its executable permission (chmod +x) or reinstall the matching Linux DFHack release.")
    raise LiveBridgeError(
        f"No {system} DFHack runner found in {root}. Expected " + " or ".join(candidates) +
        ". Install DFHack matching this game and host platform.")


def _positive_seconds(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers may briefly hold a handle without delete sharing.
        # Keep the new file intact and retry the atomic rename; never truncate
        # a published request while DFHack could be reading it.
        rename_deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= rename_deadline:
                    raise
                time.sleep(0.005)
    finally:
        temporary.unlink(missing_ok=True)


class LiveBridge:
    """One sequential controller session; snapshots contain native measurements.

    ``start_command()`` returns a trusted-host command to run once after
    ``install_script()``. The class does not launch DF or invoke arbitrary
    commands. Keep one instance for the entire run; session UUIDs and sequence
    numbers prevent old responses from being mistaken for current results.
    """

    def __init__(self, game_dir: Path | str, *, timeout: float = 30.0,
                 poll_interval: float = 0.05):
        self.game_dir = Path(game_dir).expanduser().resolve()
        if not self.game_dir.is_dir():
            raise ValueError(f"Game directory does not exist: {self.game_dir}")
        self.timeout = _positive_seconds(timeout, "timeout")
        self.poll_interval = _positive_seconds(poll_interval, "poll_interval")
        self.session = uuid.uuid4().hex
        self.ipc_dir = self.game_dir / "dfhack-config" / "dfeval-live" / self.session
        self.ipc_dir.mkdir(parents=True, exist_ok=False)
        self._sequence = 0
        self._lock = threading.Lock()

    def install_script(self) -> Path:
        """Install in DFHack's user script directory, outside bundled scripts."""
        bundled_scripts = self.game_dir / "hack" / "scripts"
        if not bundled_scripts.is_dir():
            raise LiveBridgeError(f"DFHack scripts directory is missing: {bundled_scripts}")
        scripts = self.game_dir / "dfhack-config" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).parent / "bridge" / "lua" / "dfeval-live.lua"
        destination = scripts / "dfeval-live.lua"
        shutil.copyfile(source, destination)
        return destination

    def start_command(self) -> list[str]:
        return [str(resolve_dfhack_runner(self.game_dir)), "dfeval-live", "start", self.session]

    def status(self) -> dict[str, Any]:
        return self._request("status")

    def observe(self) -> dict[str, Any]:
        return self._request("observe")

    def pause(self) -> dict[str, Any]:
        """Pause the game. Advancing is the only API that unpauses it."""
        return self._request("pause")

    def advance_ticks(self, ticks: int, *, timeout: float | None = None) -> dict[str, Any]:
        return self._request("advance_ticks", {"ticks": ticks}, timeout=timeout)

    def set_simulation_fps(self, fps: int) -> dict[str, Any]:
        """Trusted host only: set a bounded native simulation cap, leaving GFPS alone.

        This is not a model action. The session retains its original cap and
        restores it on advance failure, cancellation, or world/session change.
        The host must call restore_simulation_fps at normal experiment cleanup.
        A higher cap does not promise that the hardware can achieve that rate.
        """
        return self._request("set_simulation_fps", {"fps": fps})

    def restore_simulation_fps(self) -> dict[str, Any]:
        """Return native readback and an explicit restored flag; idempotent."""
        return self._request("restore_simulation_fps")

    def queue_brew(self, workshop_id: int, quantity: int = 1) -> dict[str, Any]:
        """Queue 1–10 normal brew-from-plant jobs at an existing completed still.

        Quantity counts jobs, not guaranteed drink output. Native labor,
        ingredients, containers, access, and job cancellation rules still apply.
        """
        return self._request("queue_brew", {"workshop_id": workshop_id, "quantity": quantity})

    def _request(self, operation: str, arguments: dict[str, Any] | None = None,
                 *, timeout: float | None = None) -> dict[str, Any]:
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"Unsupported live operation: {operation!r}")
        args = {} if arguments is None else dict(arguments)
        expected = {"advance_ticks": {"ticks"}, "queue_brew": {"workshop_id", "quantity"},
                    "set_simulation_fps": {"fps"}}
        if set(args) != expected.get(operation, set()):
            raise ValueError(f"Invalid arguments for {operation}")
        if operation == "advance_ticks":
            _integer(args["ticks"], "ticks", 1, MAX_ADVANCE_TICKS)
        if operation == "queue_brew":
            _integer(args["workshop_id"], "workshop_id", 0, 2**31 - 1)
            _integer(args["quantity"], "quantity", 1, 10)
        if operation == "set_simulation_fps":
            _integer(args["fps"], "fps", 1, MAX_SIMULATION_FPS)
        wait = self.timeout if timeout is None else _positive_seconds(timeout, "timeout")
        if wait > 3600:
            raise ValueError("timeout must not exceed 3600 seconds")
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            request = {"protocol": PROTOCOL_VERSION, "session": self.session,
                       "seq": sequence, "op": operation, "args": args,
                       "timeout_ms": max(1, math.ceil(wait * 1000)),
                       "expires_at": time.time() + wait}
            response_path = self.ipc_dir / f"response.{sequence}.json"
            def cancel() -> None:
                _atomic_json(self.ipc_dir / f"cancel.{sequence}.json",
                             {"session": self.session, "seq": sequence})

            try:
                deadline = time.monotonic() + wait
                _atomic_json(self.ipc_dir / "request.json", request)
                while True:
                    if response_path.exists():
                        try:
                            with response_path.open("rb") as response_stream:
                                encoded = response_stream.read(MAX_RESPONSE_BYTES + 1)
                            if len(encoded) > MAX_RESPONSE_BYTES:
                                raise ValueError("Native response exceeds the transport byte limit")
                            response = json.loads(encoded.decode("utf-8"))
                        except PermissionError as error:
                            # Windows can expose the renamed path before another
                            # process releases its sharing lock. Retry within the
                            # same request deadline instead of rejecting valid data.
                            remaining = deadline - time.monotonic()
                            if remaining > 0:
                                time.sleep(min(self.poll_interval, remaining))
                                continue
                            cancel()
                            raise LiveBridgeTimeout(
                                f"Could not read {operation} response after {wait:g}s; cancellation requested. "
                                "Check the game and DFHack; pause is not yet confirmed.") from error
                        except (OSError, ValueError, RecursionError) as error:
                            cancel()
                            raise LiveBridgeError(f"Invalid response file: {error}") from error
                        if not isinstance(response, dict) or any(
                            response.get(key) != value for key, value in (
                                ("protocol", PROTOCOL_VERSION), ("session", self.session),
                                ("seq", sequence), ("op", operation))
                        ) or type(response.get("seq")) is not int or type(response.get("protocol")) is not int:
                            cancel()
                            raise LiveBridgeError("Rejected stale or mismatched live response")
                        if type(response.get("ok")) is not bool:
                            cancel()
                            raise LiveBridgeError("Response is missing a boolean ok field")
                        if not response["ok"]:
                            raise LiveBridgeError(str(response.get("error", "Unknown game error")))
                        if not isinstance(response.get("result"), dict):
                            cancel()
                            raise LiveBridgeError("Response result must be an object")
                        return response["result"]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        cancel()
                        raise LiveBridgeTimeout(
                            f"No response to {operation} after {wait:g}s; cancellation requested. "
                            "Check the game and DFHack; pause is not yet confirmed.")
                    time.sleep(min(self.poll_interval, remaining))
            except KeyboardInterrupt as interrupted:
                # Target this request's captured sequence even when the runner
                # immediately follows with a new pause request. While advancing,
                # Lua reads cancel.<active seq>.json before accepting fresh work.
                try:
                    cancel()
                except OSError:
                    interrupted.add_note(
                        "Could not publish bridge cancellation; pause is not yet confirmed.")
                raise
