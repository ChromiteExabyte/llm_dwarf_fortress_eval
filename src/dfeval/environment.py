"""Read-only, bounded evidence of local installation configuration bytes.

This is a drift detector for repeats on an installation, not an assertion that
these files are effective native settings or a complete simulation environment.
No native game calls, configuration writes, or external-path reads are made.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from typing import Any


class EnvironmentCaptureError(ValueError):
    """The selected installation files cannot be completely fingerprinted."""


MAX_FILES = 20_000
MAX_DIRECTORIES = 20_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_CUSTOM_PATHS = 64
MAX_SCRIPT_PATHS_BYTES = 256 * 1024

_FILE_ROOTS = (
    "prefs/init.txt", "prefs/d_init.txt", "prefs/announcements.txt",
    "data/init/init.txt", "data/init/d_init.txt", "data/init/announcements.txt",
    "data/init/init_default.txt", "data/init/d_init_default.txt",
    "dfhack-config/script-paths.txt", "dfhack-config/settings-manager.json",
    "dfhack-config/control-panel.json",
)
_DIRECTORY_ROOTS = (
    "dfhack-config/init", "dfhack-config/scripts", "dfhack-config/mods",
    "init.d", "hack/init", "hack/scripts", "raw", "mods",
    "data/installed_mods", "data/vanilla",
)
_EXCLUDED_COMPONENTS = frozenset({
    ".git", "__pycache__", "save", "saves", "backup", "backups",
    "models", "logs", "runs", "dfeval-live", "dfeval-setup", "dfeval-backups",
})
_GENERATED_BRIDGES = frozenset({
    "hack/scripts/dfeval-live.lua", "dfhack-config/scripts/dfeval-live.lua",
})
_NORMALIZED_SCRIPT_ROOTS = frozenset({"hack/scripts", "dfhack-config/scripts"})
_CONTENT_SUFFIXES = frozenset({
    ".txt", ".lua", ".rb", ".json", ".init", ".xml", ".cfg", ".ini",
    ".toml", ".yaml", ".yml", ".csv",
})
_LIMITATIONS = [
    "Files are known configuration and code candidates, not verified effective native settings.",
    "Preferences outside the game directory and external custom script paths are not read.",
    "Native active tools, timers, difficulty, labor assignments, loaded mods, and runtime settings are not observed.",
    "DF and DFHack binaries, external Workshop mods, and unlisted plugin configuration are outside this inventory.",
    "Directory inventories select declared text/config/script suffixes; binary game assets are not hashed. The generated live bridge has a separate fingerprint.",
    "Absent and empty default script roots are equivalent, so installing only the generated live bridge does not create settings drift.",
    "Save data, logs, backups, models, run artifacts, and transient IPC are excluded; the save has a separate fingerprint.",
    "Capture is not an atomic filesystem snapshot; detected changes during a read fail, but concurrent later changes remain possible.",
    "Matching these bytes detects no drift within the declared scope; it does not establish deterministic simulation.",
]


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _check_ancestors(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise EnvironmentCaptureError("Symlinks and filesystem reparse points are not allowed in selected paths")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def recorded_environment_fingerprint(value: Any) -> str:
    """Verify a recorded inventory's checksum without inspecting an installation."""
    if (not isinstance(value, dict) or value.get("kind") != "local_installation_environment"
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not all(isinstance(value.get(key), list) for key in
                       ("files", "directories", "missing_roots", "custom_script_paths", "limitations"))
            or not isinstance(value.get("scope"), dict)):
        raise EnvironmentCaptureError("Missing or malformed recorded environment inventory")
    digest = hashlib.sha256(_canonical({key: item for key, item in value.items()
                                       if key != "fingerprint_sha256"})).hexdigest()
    if value.get("fingerprint_sha256") != digest:
        raise EnvironmentCaptureError("Recorded environment inventory checksum mismatch")
    return digest


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _excluded(relative: str) -> bool:
    path = PurePosixPath(relative)
    return any(part.casefold() in _EXCLUDED_COMPONENTS for part in path.parts) or path.suffix.casefold() == ".log"


class _Inventory:
    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, dict[str, Any]] = {}
        self.directories: set[str] = set()
        self.missing: set[str] = set()
        self.seen: dict[str, str] = {}
        self.total_bytes = 0
        self.script_paths: bytes | None = None

    def _register(self, relative: str) -> None:
        if any(part in {"", ".", ".."} or "\\" in part or ":" in part
               for part in relative.split("/")):
            raise EnvironmentCaptureError("Selected paths must have portable relative names")
        previous = self.seen.setdefault(relative.casefold(), relative)
        if previous != relative:
            raise EnvironmentCaptureError("Selected paths have a case-insensitive collision")

    def add(self, relative: str, *, expected: str | None = None) -> None:
        # These generated copies are installed after environment capture. The
        # runner independently fingerprints their source as bridge_sha256.
        if relative.casefold() in _GENERATED_BRIDGES:
            return
        self._register(relative)
        if _excluded(relative):
            return
        path = self.root / relative
        _check_ancestors(path)
        try:
            before = path.lstat()
        except FileNotFoundError:
            self.missing.add(relative)
            return
        if _is_link(before):
            raise EnvironmentCaptureError("Selected tree contains a symlink or reparse point")
        if stat.S_ISDIR(before.st_mode):
            if expected == "file":
                raise EnvironmentCaptureError(f"Expected a configuration file: {relative}")
            if relative in self.directories:
                return
            if len(self.directories) >= MAX_DIRECTORIES:
                raise EnvironmentCaptureError("Environment directory count exceeds the capture budget")
            self.directories.add(relative)
            # Bound enumeration itself: a directory with excessive entries does
            # not first allocate an unbounded list for sorting.
            count = 0
            with os.scandir(path) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_FILES + MAX_DIRECTORIES:
                        raise EnvironmentCaptureError("Environment entry count exceeds the capture budget")
                    self.add(f"{relative}/{entry.name}")
            if _signature(before) != _signature(path.lstat()):
                raise EnvironmentCaptureError(f"Directory changed during capture: {relative}")
        elif stat.S_ISREG(before.st_mode):
            if expected == "directory":
                raise EnvironmentCaptureError(f"Expected a configuration directory: {relative}")
            if relative in self.files:
                return
            if expected != "file" and path.suffix.casefold() not in _CONTENT_SUFFIXES:
                return
            if len(self.files) >= MAX_FILES:
                raise EnvironmentCaptureError("Environment file count exceeds the capture budget")
            limit = MAX_SCRIPT_PATHS_BYTES if relative == "dfhack-config/script-paths.txt" else MAX_FILE_BYTES
            if before.st_size > min(limit, MAX_FILE_BYTES):
                raise EnvironmentCaptureError(f"File exceeds the per-file capture budget: {relative}")
            if self.total_bytes + before.st_size > MAX_TOTAL_BYTES:
                raise EnvironmentCaptureError("Environment bytes exceed the total capture budget")
            digest, size, kept = hashlib.sha256(), 0, []
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or _is_link(opened) or _signature(opened) != _signature(before):
                    raise EnvironmentCaptureError(f"File changed before reading: {relative}")
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > min(limit, MAX_FILE_BYTES) or self.total_bytes + size > MAX_TOTAL_BYTES:
                        raise EnvironmentCaptureError("File grew beyond the capture byte budget")
                    digest.update(chunk)
                    if relative == "dfhack-config/script-paths.txt":
                        kept.append(chunk)
                after = os.fstat(stream.fileno())
            if size != before.st_size or _signature(before) != _signature(after) or _signature(before) != _signature(path.lstat()):
                raise EnvironmentCaptureError(f"File changed during capture: {relative}")
            self.files[relative] = {"path": relative, "size": size, "sha256": digest.hexdigest()}
            self.total_bytes += size
            if relative == "dfhack-config/script-paths.txt":
                self.script_paths = b"".join(kept)
        else:
            raise EnvironmentCaptureError(f"Selected tree contains a nonregular file: {relative}")

    def custom_paths(self) -> list[dict[str, Any]]:
        if self.script_paths is None:
            return []
        try:
            contents = self.script_paths.decode("utf-8-sig")
        except UnicodeError as exc:
            raise EnvironmentCaptureError("script-paths.txt must be UTF-8 to audit custom paths") from exc
        result = []
        for number, line in enumerate(contents.splitlines(), 1):
            declaration = line.strip()
            if not declaration or declaration.startswith("#"):
                continue
            if len(result) >= MAX_CUSTOM_PATHS:
                raise EnvironmentCaptureError("Custom script path count exceeds the capture budget")
            record: dict[str, Any] = {"line": number,
                "declaration_sha256": hashlib.sha256(declaration.encode("utf-8")).hexdigest()}
            result.append(record)
            if declaration[0] not in "+-" or not declaration[1:].strip() or "\x00" in declaration:
                record["status"] = "uncovered_invalid_declaration"
                continue
            record["priority"] = "before" if declaration[0] == "+" else "after"
            name = declaration[1:].strip()
            if ".." in PurePosixPath(name.replace("\\", "/")).parts:
                record["status"] = "uncovered_traversal_path"
                continue
            candidate = Path(name)
            if PureWindowsPath(name).drive and os.name != "nt":
                record["status"] = "uncovered_external_path"
                continue
            if not candidate.is_absolute():
                candidate = self.root / candidate
            # Lexically normalize traversal before containment, without following
            # a link. Internal paths are then checked component by component.
            candidate = Path(os.path.abspath(candidate))
            if not candidate.is_relative_to(self.root):
                record["status"] = "uncovered_external_path"
                continue
            relative = candidate.relative_to(self.root).as_posix()
            if relative == "." or relative in {"data", "dfhack-config", "hack"}:
                record["status"] = "uncovered_overbroad_path"
                continue
            record["path"] = relative
            if _excluded(relative):
                record["status"] = "uncovered_excluded_path"
                continue
            self.add(relative, expected="directory")
            if relative in _NORMALIZED_SCRIPT_ROOTS:
                record["status"] = "captured_optional_script_root"
            else:
                record["status"] = "missing" if relative in self.missing else "captured"
        return result


def capture_environment(game_dir: str | Path) -> dict[str, Any]:
    """Hash selected local settings/code without reading saves or calling DF.

    Missing candidate paths are fingerprinted too. Returned metadata contains
    only paths relative to the game directory; external custom declarations are
    represented by a digest and an explicit uncovered status, never traversed.
    A partial inventory is never returned on permission, link, or budget errors.
    """
    try:
        root = Path(game_dir).expanduser().absolute()
        if str(root.anchor).startswith("\\\\"):
            raise EnvironmentCaptureError("Environment capture requires a local game directory")
        _check_ancestors(root)
        root = root.resolve()
        if not root.is_dir():
            raise EnvironmentCaptureError("Game directory must be an existing directory")
        inventory = _Inventory(root)
        for relative in _FILE_ROOTS:
            inventory.add(relative, expected="file")
        for relative in _DIRECTORY_ROOTS:
            inventory.add(relative, expected="directory")
        custom_paths = inventory.custom_paths()
        result: dict[str, Any] = {
            "schema_version": 1,
            "kind": "local_installation_environment",
            "files": sorted(inventory.files.values(), key=lambda item: item["path"]),
            "directories": sorted(inventory.directories - _NORMALIZED_SCRIPT_ROOTS),
            "missing_roots": sorted(inventory.missing - _NORMALIZED_SCRIPT_ROOTS),
            "custom_script_paths": custom_paths,
            "file_count": len(inventory.files),
            "total_bytes": inventory.total_bytes,
            "scope": {
                "file_candidates": list(_FILE_ROOTS),
                "directory_candidates": list(_DIRECTORY_ROOTS),
                "custom_paths": "Game-internal directories only; external declarations are recorded but not read.",
                "excluded_directory_names": sorted(_EXCLUDED_COMPONENTS),
                "excluded_file_suffixes": [".log"],
                "included_directory_file_suffixes": sorted(_CONTENT_SUFFIXES),
                "excluded_generated_files": sorted(_GENERATED_BRIDGES),
                "normalized_optional_script_roots": sorted(_NORMALIZED_SCRIPT_ROOTS),
            },
            "limitations": list(_LIMITATIONS),
        }
        result["fingerprint_sha256"] = hashlib.sha256(_canonical(result)).hexdigest()
        return result
    except EnvironmentCaptureError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        # Avoid leaking a user-specific external path from an OS exception into
        # portable run evidence. The exception chain remains available locally.
        raise EnvironmentCaptureError("Cannot completely inspect the selected installation environment") from exc
