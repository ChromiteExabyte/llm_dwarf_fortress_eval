"""Inspect a local game installation without launching or contacting it.

Finding files is not a connectivity test. In particular, a save directory
does not establish that a fortress is loaded, and an old bridge response does
not establish that a bridge is running. Live checks belong to the bridge.
"""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path
from typing import Any

from .live import LiveBridgeError, resolve_dfhack_runner


DEFAULT_DF_PATH = Path("dwarfFortressItself")
_EXECUTABLES = (
    "Dwarf Fortress.exe", "Dwarf_Fortress.exe", "dwarfort.exe",
    "Dwarf_Fortress", "Dwarf Fortress", "dwarfort", "df", "libs/Dwarf_Fortress",
)
_CORE_FILES = (
    "dfhack.dll", "hack/dfhack.dll", "hack/libdfhack.so",
    "libdfhack.so", "hack/libdfhack.dylib", "libdfhack.dylib",
)
_VERSION_FILES = (
    "hack/dfhack-version.txt", "hack/dfhack_version.txt", "hack/version.txt",
    "dfhack-version.txt", "dfhack_version.txt", "dfhack.version",
    "hack/dfhack.version",
    "hack/news.rst", "hack/docs/docs/NEWS.txt",
)


def _first_file(root: Path, names: tuple[str, ...]) -> Path | None:
    return next((root / name for name in names if (root / name).is_file()), None)


def _read_text(path: Path | None, errors: list[str]) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        errors.append(f"Cannot read {path.name}: {exc}")
        return ""


def _save_inventory(root: Path, errors: list[str]) -> list[dict[str, Any]]:
    saves = []
    for location in (root / "save", root / "data" / "save"):
        if not location.is_dir():
            continue
        try:
            entries = sorted(location.iterdir(), key=lambda path: path.name.casefold())
        except OSError as exc:
            errors.append(f"Cannot inspect {location}: {exc}")
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name == "current":
                continue
            world_file = entry / "world.sav"
            saves.append({
                "name": entry.name,
                "path": str(entry),
                "world_file": str(world_file) if world_file.is_file() else None,
                "has_world_save": world_file.is_file(),
                "fortress_verified": False,
            })
    return saves


def inspect_installation(df_path: str | Path = DEFAULT_DF_PATH) -> dict[str, Any]:
    """Return JSON-ready file evidence; never run the game or read bridge replies.

    ``ready_for_live_probe`` means the executable, DFHack core/support files,
    host runner, and bridge script were found. It does not verify binary compatibility,
    script contents, connectivity, save validity, or gameplay readiness.
    """
    root = Path(df_path).expanduser().resolve()
    errors: list[str] = []
    executable = _first_file(root, _EXECUTABLES)
    release_notes = _first_file(root, ("release notes.txt", "release_notes.txt"))
    # The preamble mentions older compatible versions before the first release
    # heading. Match the heading, rather than the first version-like number.
    match = re.search(
        r"(?im)^\s*Release notes for\s+(\d+(?:\.\d+){1,2})\b",
        _read_text(release_notes, errors),
    )
    game_version = match.group(1) if match else None
    core_files = [str(root / name) for name in _CORE_FILES if (root / name).is_file()]
    support_directory = root / "hack"
    support_present = support_directory.is_dir()
    version_file = _first_file(root, _VERSION_FILES)
    version_match = re.search(
        r"(?im)^\s*(?:DFHack\s+)?v?(\d+(?:\.\d+){1,2}(?:[-.][A-Za-z0-9]+)*)\s*$",
        _read_text(version_file, errors),
    )
    dfhack_version = version_match.group(1) if version_match else None
    script_directories = ("dfhack-config/scripts", "hack/scripts")
    live_bridge_paths = [root / directory / "dfeval-live.lua" for directory in script_directories
                         if (root / directory / "dfeval-live.lua").is_file()]
    legacy_bridge_paths = [root / directory / "dfeval-bridge.lua" for directory in script_directories
                           if (root / directory / "dfeval-bridge.lua").is_file()]
    live_bridge_installed = bool(live_bridge_paths)
    legacy_bridge_installed = bool(legacy_bridge_paths)
    bridge_installed = live_bridge_installed or legacy_bridge_installed
    bridge_path = (live_bridge_paths + legacy_bridge_paths or
                   [root / "dfhack-config" / "scripts" / "dfeval-live.lua"])[0]
    dfhack_files_present = bool(core_files) and support_present
    runner, runner_error = None, None
    try:
        runner = resolve_dfhack_runner(root)
    except LiveBridgeError as exc:
        runner_error = str(exc)
    saves = _save_inventory(root, errors)
    save_found = any(save["has_world_save"] for save in saves)

    next_steps = []
    if executable is None:
        next_steps.append("Point --df-path at a Dwarf Fortress installation containing the game executable.")
    if not dfhack_files_present:
        next_steps.append("Install DFHack matching this Dwarf Fortress version; core and hack/ support files are required.")
    if runner_error:
        next_steps.append(runner_error)
    if not live_bridge_installed:
        next_steps.append("Install dfeval-live.lua into the game's dfhack-config/scripts directory.")
    if not save_found:
        next_steps.append("Create a world and embark, or copy a prepared save into this installation.")
    next_steps.append("Launch the game with DFHack, load a fortress, then run a live bridge check.")

    return {
        "path": str(root),
        "inspection": "offline",
        "host_platform": platform.system(),
        "game": {
            "executable": str(executable) if executable else None,
            "version": game_version,
            "version_source": str(release_notes) if game_version else None,
        },
        "dfhack": {
            "present": bool(core_files) or support_present,
            "core_files": core_files,
            "support_directory": str(support_directory) if support_present else None,
            "version": dfhack_version,
            "version_source": str(version_file) if dfhack_version else None,
            "version_evidence": (
                "bundled_documentation" if version_file and version_file.name in ("news.rst", "NEWS.txt")
                else "version_file"
            ) if dfhack_version else None,
            "compatibility_verified": False,
            "runner": str(runner) if runner else None,
            "runner_error": runner_error,
        },
        "bridge": {
            "installed": bridge_installed,
            "path": str(bridge_path),
            "live_installed": live_bridge_installed,
            "legacy_installed": legacy_bridge_installed,
            "live_paths": [str(path) for path in live_bridge_paths],
            "legacy_paths": [str(path) for path in legacy_bridge_paths],
        },
        "saves": saves,
        "readiness": {
            "game_files_present": executable is not None,
            "dfhack_files_present": dfhack_files_present,
            "bridge_installed": bridge_installed,
            "live_bridge_installed": live_bridge_installed,
            "host_runner_available": runner is not None,
            "save_candidates_present": save_found,
            "ready_for_live_probe": executable is not None and dfhack_files_present
                                    and live_bridge_installed and runner is not None,
            "connected": None,
            "fortress_loaded": None,
            "playable": None,
        },
        "errors": errors,
        "next_steps": next_steps,
    }


def report_json(report: dict[str, Any]) -> str:
    """Render the complete evidence for scripting or a diagnostic attachment."""
    return json.dumps(report, indent=2, ensure_ascii=False)


def format_report(report: dict[str, Any]) -> str:
    """Render the same findings in plain language, with unknowns explicit."""
    game, hack, readiness = report["game"], report["dfhack"], report["readiness"]
    lines = [
        f"Dwarf Fortress installation: {report['path']}",
        "Offline inspection only; the game has not been contacted.",
        f"Game executable: {game['executable'] or 'not found'}",
        f"Game version from release notes: {game['version'] or 'unknown'}",
        f"DFHack core/support files: {'found' if readiness['dfhack_files_present'] else 'incomplete or not found'}",
        f"DFHack version from files: {hack['version'] or 'unknown'} (compatibility not verified)",
        f"DFHack runner for {report['host_platform']}: {hack['runner'] or 'not available'}",
        f"Live bridge script: {'found' if report['bridge']['live_installed'] else 'not found'}",
        f"Legacy bridge script: {'found' if report['bridge']['legacy_installed'] else 'not found'}",
    ]
    if hack["version_source"]:
        lines.append(f"DFHack version evidence: {hack['version_source']}")
    verified_files = sum(save["has_world_save"] for save in report["saves"])
    lines.append(f"Save folders containing world.sav: {verified_files} (fortress contents not verified)")
    for save in report["saves"]:
        detail = "world.sav found" if save["has_world_save"] else "no world.sav found"
        lines.append(f"  {save['name']}: {detail}")
    lines.extend([
        f"Files ready for a live probe: {'yes' if readiness['ready_for_live_probe'] else 'no'}",
        "Connected: unknown (requires a live check)",
        "Fortress loaded: unknown (requires a live check)",
        "Playable: unknown (requires a live action/observation test)",
    ])
    lines.extend(f"Inspection error: {error}" for error in report["errors"])
    lines.append("Next steps:")
    lines.extend(f"  {index}. {step}" for index, step in enumerate(report["next_steps"], 1))
    return "\n".join(lines)
