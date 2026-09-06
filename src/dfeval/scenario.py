"""Trusted-operator snapshots of saved native worlds, separate from public specs.

Save bytes stay local. Capture/restore require an explicit saved-and-stopped
attestation and a process check; neither starts, stops, or edits a running game.
Verification covers every saved file and directory. It establishes byte identity,
not binary-version compatibility or the gameplay content of a world.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from .doctor import inspect_installation


class ScenarioError(RuntimeError):
    """A save snapshot is invalid or cannot safely be captured/restored."""


_MANIFEST_KEYS = {"schema_version", "kind", "save_name", "df_version", "created_utc",
                  "files", "directories", "file_count", "total_bytes"}
_GAME_NAMES = {"dwarf fortress.exe", "dwarf_fortress.exe", "dwarfort.exe",
               "dwarf fortress", "dwarf_fortress", "dwarfort"}


def _save_name(value: str) -> str:
    if (not isinstance(value, str) or not value or value in {".", ".."}
            or any(character in value for character in '/\\:\x00')
            or value.endswith((".", " ")) or Path(value).name != value):
        raise ScenarioError("save_name must be one plain directory basename, without traversal or separators")
    return value


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _path(value: str | Path) -> Path:
    # Check the lexical path before resolve(), which would hide symlinks.
    candidate = Path(value).expanduser().absolute()
    if str(candidate.anchor).startswith("\\\\"):
        raise ScenarioError("Scenario operations require a local filesystem path, not a UNC share")
    for part in (candidate, *candidate.parents):
        if os.path.lexists(part) and _is_link(part):
            raise ScenarioError(f"Symlinks and filesystem reparse points are not allowed: {part}")
    return candidate.resolve()


def _directory(value: str | Path, label: str) -> Path:
    path = _path(value)
    if not path.is_dir():
        raise ScenarioError(f"{label} is not an existing directory: {path}")
    return path


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _running_game_processes() -> list[str]:
    """Conservative process-name check; not an OS lock against future launches."""
    system = platform.system()
    if system == "Windows":
        try:
            result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                    text=True, errors="replace", timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScenarioError(f"Cannot check running games with tasklist: {exc}") from exc
        if result.returncode:
            raise ScenarioError("tasklist could not verify whether Dwarf Fortress is running")
        return [row[0] for row in csv.reader(io.StringIO(result.stdout))
                if row and row[0].casefold() in _GAME_NAMES]
    if system == "Linux":
        processes = Path("/proc")
        if not processes.is_dir():
            raise ScenarioError("Linux /proc is unavailable; cannot check whether the game is stopped")
        found = []
        try:
            entries = list(processes.iterdir())
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                except FileNotFoundError:
                    continue  # Process exited between enumeration and read.
                except PermissionError as exc:
                    raise ScenarioError("Cannot inspect all visible processes; game-stopped check failed") from exc
                if name.casefold() in _GAME_NAMES:
                    found.append(name)
        except OSError as exc:
            raise ScenarioError(f"Cannot inspect Linux /proc: {exc}") from exc
        return found
    raise ScenarioError("Save capture/restore process checks support Windows and Linux only")


def _require_stopped(game_stopped: bool) -> None:
    if game_stopped is not True:
        raise ScenarioError("Save and fully exit Dwarf Fortress first, then explicitly set game_stopped=True (--game-stopped)")
    running = _running_game_processes()
    if running:
        raise ScenarioError("Dwarf Fortress is still running; save and fully exit it first: " + ", ".join(running))


def _relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ScenarioError("Manifest paths must be nonempty portable relative paths")
    pieces = value.split("/")
    if any(piece in {"", ".", ".."} or piece.endswith((".", " ")) for piece in pieces):
        raise ScenarioError("Manifest path contains traversal or a nonportable component")
    if PurePosixPath(value).is_absolute():
        raise ScenarioError("Absolute manifest paths are forbidden")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> dict[str, Any]:
    _directory(root, "Save tree")
    files, directories, seen = [], [], set()
    def inaccessible(error: OSError) -> None:
        raise ScenarioError(f"Cannot inspect the complete save tree: {error}")

    for current, children, names in os.walk(root, followlinks=False, onerror=inaccessible):
        for name in sorted(children + names):
            path = Path(current) / name
            if _is_link(path):
                raise ScenarioError(f"Save tree contains a symlink or reparse point: {path}")
            relative = _relative_name(path.relative_to(root).as_posix())
            if relative.casefold() in seen:
                raise ScenarioError(f"Save tree has a case-insensitive path collision: {relative}")
            seen.add(relative.casefold())
            info = path.stat()
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode):
                files.append({"path": relative, "size": info.st_size, "sha256": _hash(path)})
            else:
                raise ScenarioError(f"Save tree contains a nonregular file: {relative}")
    files.sort(key=lambda item: item["path"])
    directories.sort()
    return {"files": files, "directories": directories, "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files)}


def _validate_manifest(manifest: Any) -> None:
    if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or manifest.get("kind") != "dfeval_native_save_snapshot"):
        raise ScenarioError("Unsupported or malformed native save snapshot manifest")
    _save_name(manifest["save_name"])
    if manifest["df_version"] is not None and not isinstance(manifest["df_version"], str):
        raise ScenarioError("Manifest df_version must be a string or null")
    if not isinstance(manifest["created_utc"], str):
        raise ScenarioError("Manifest created_utc must be a string")
    if not isinstance(manifest["files"], list) or not isinstance(manifest["directories"], list):
        raise ScenarioError("Manifest file and directory inventories must be arrays")
    seen, total = set(), 0
    for directory in manifest["directories"]:
        key = _relative_name(directory).casefold()
        if key in seen:
            raise ScenarioError("Manifest contains a duplicate path")
        seen.add(key)
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ScenarioError("Malformed manifest file entry")
        key = _relative_name(item["path"]).casefold()
        if key in seen:
            raise ScenarioError("Manifest contains a duplicate path")
        seen.add(key)
        if type(item["size"]) is not int or item["size"] < 0:
            raise ScenarioError("Manifest file size must be a nonnegative integer")
        checksum = item["sha256"]
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ScenarioError("Manifest SHA256 must have 64 lowercase hexadecimal characters")
        total += item["size"]
    if (type(manifest["file_count"]) is not int or manifest["file_count"] != len(manifest["files"])
            or type(manifest["total_bytes"]) is not int or manifest["total_bytes"] != total):
        raise ScenarioError("Manifest file count or byte total does not match its inventory")
    if not any(item["path"] == "world.sav" for item in manifest["files"]):
        raise ScenarioError("Snapshot must contain world.sav at its save root")


def _matches(manifest: dict[str, Any], inventory: dict[str, Any]) -> bool:
    return all(manifest[key] == inventory[key] for key in ("files", "directories", "file_count", "total_bytes"))


def verify_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    """Return the public manifest only after checking complete local save bytes."""
    root = _directory(snapshot_dir, "Snapshot")
    if {entry.name for entry in root.iterdir()} != {"manifest.json", "save"}:
        raise ScenarioError("Snapshot must contain exactly manifest.json and the save directory")
    manifest_path = _path(root / "manifest.json")
    try:
        if manifest_path.stat().st_size > 16 * 1024 * 1024:
            raise ScenarioError("Snapshot manifest is too large")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"Cannot read snapshot manifest: {exc}") from exc
    _validate_manifest(manifest)
    if not _matches(manifest, _inventory(root / "save")):
        raise ScenarioError("Snapshot integrity check failed: files, directories, sizes, or SHA256 hashes differ")
    return manifest


def compatible_df_versions(first: Any, second: Any) -> bool:
    """Compare release-note and native display versions without rewriting either.

    DF 53.16 identifies itself natively as e.g. ``v0.53.16 win64 ITCH``.
    Only these recognized game-version decorations are normalized. DFHack
    revisions and the initial-state guard keep their exact recorded identities.
    """
    if not isinstance(first, str) or not isinstance(second, str) or not first or not second:
        return False
    if first == second:
        return True
    pattern = (r"v?(?:0\.)?([0-9]{2,3})\.([0-9]{2})"
               r"(?: (?:win32|win64|linux32|linux64|osx32|osx64|macos|macos64) (?:ITCH|STEAM|CLASSIC))?")
    matches = [re.fullmatch(pattern, value, flags=re.IGNORECASE) for value in (first, second)]
    return all(matches) and matches[0].groups() == matches[1].groups()


def _save_roots(game: Path, save_root: str | Path | None = None) -> tuple[Path, ...]:
    # Explicit opt-in is required for per-user/global save storage. Never guess
    # APPDATA/HOME locations: they can contain unrelated installations' worlds.
    if save_root is not None:
        if isinstance(save_root, str) and not save_root.strip():
            raise ScenarioError("Explicit save root must be a nonempty directory path")
        return (_directory(save_root, "Explicit save root"),)
    return _path(game / "save"), _path(game / "data" / "save")


def _cleanup_stage(stage: Path, parent: Path, prefix: str) -> None:
    """Delete only a temporary directory this operation allocated in its parent."""
    if not os.path.lexists(stage):
        return
    checked = _path(stage)
    if checked.parent != parent or not checked.name.startswith(prefix):
        raise ScenarioError("Refusing cleanup outside the operation's temporary staging directory")
    shutil.rmtree(checked)


def snapshot_save(game_dir: str | Path, save_name: str, destination: str | Path,
                  *, game_stopped: bool = False, save_root: str | Path | None = None) -> dict[str, Any]:
    """Copy one completed save into a new immutable-by-convention local snapshot.

    The source stays untouched. The public manifest contains no absolute paths;
    the accompanying save bytes must remain excluded from source publication.
    save_root explicitly selects an existing directory containing save folders;
    otherwise only game/save and game/data/save are inspected.
    """
    game = _directory(game_dir, "Game installation")
    name = _save_name(save_name)
    target = _path(destination)
    _require_stopped(game_stopped)
    roots = _save_roots(game, save_root)
    candidates = [_path(root / name) for root in roots if (root / name).is_dir()]
    if len(candidates) != 1:
        raise ScenarioError("Expected exactly one named save in the selected save root(s); use --save-root for saves outside game/save or game/data/save")
    source = candidates[0]
    if not (source / "world.sav").is_file():
        raise ScenarioError("Named save does not contain world.sav")
    if any(_overlap(target, root) for root in roots):
        raise ScenarioError("Snapshot destination must be outside the game's save roots")
    if os.path.lexists(target):
        raise ScenarioError("Snapshot destination already exists; choose a new directory")
    inventory = _inventory(source)
    manifest = {"schema_version": 1, "kind": "dfeval_native_save_snapshot", "save_name": name,
                "df_version": inspect_installation(game)["game"]["version"],
                "created_utc": datetime.now(timezone.utc).isoformat(), **inventory}
    _validate_manifest(manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".dfeval-capture-", dir=target.parent))
    try:
        shutil.copytree(source, stage / "save", symlinks=True)
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        verify_snapshot(stage)
        if not _matches(manifest, _inventory(source)):
            raise ScenarioError("Source save changed during capture; fully save and exit the game before retrying")
        _require_stopped(game_stopped)
        if os.path.lexists(target):
            raise ScenarioError("Snapshot destination appeared during capture; refusing to replace it")
        os.rename(stage, target)
    finally:
        _cleanup_stage(stage, target.parent, ".dfeval-capture-")
    return manifest


def restore_save(snapshot_dir: str | Path, game_dir: str | Path, save_name: str | None = None,
                 backup_dir: str | Path | None = None, *, game_stopped: bool = False,
                 expected_manifest_sha256: str | None = None,
                 save_root: str | Path | None = None) -> dict[str, Any]:
    """Restore verified bytes through staging; never overwrite an existing save.

    For replacement, backup_dir names a NEW exact directory for the existing save.
    It must permit an atomic same-filesystem rename. The original is retained at
    that backup path; failed promotion rolls it back. Snapshots are never edited.
    """
    snapshot = _directory(snapshot_dir, "Snapshot")
    game = _directory(game_dir, "Game installation")
    _require_stopped(game_stopped)
    if expected_manifest_sha256 is not None and (
            not isinstance(expected_manifest_sha256, str) or len(expected_manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_manifest_sha256)):
        raise ScenarioError("Expected snapshot identity must be a lowercase SHA-256 digest")
    snapshot_hash = _hash(_path(snapshot / "manifest.json"))
    if expected_manifest_sha256 is not None and snapshot_hash != expected_manifest_sha256:
        raise ScenarioError("Snapshot manifest does not match the expected starting-save identity")
    manifest = verify_snapshot(snapshot)
    if _hash(_path(snapshot / "manifest.json")) != snapshot_hash:
        raise ScenarioError("Snapshot manifest changed during verification")
    name = _save_name(manifest["save_name"] if save_name is None else save_name)
    roots = _save_roots(game, save_root)
    existing = [_path(root / name) for root in roots if os.path.lexists(root / name)]
    if len(existing) > 1:
        raise ScenarioError("Save name exists under both supported save roots; destination is ambiguous")
    selected_root = existing[0].parent if existing else next((root for root in roots if root.is_dir()), roots[0])
    target = _path(selected_root / name)
    if _overlap(snapshot, target):
        raise ScenarioError("Restore destination overlaps the source snapshot")
    backup = _path(backup_dir) if backup_dir is not None else None
    if target.exists() and not target.is_dir():
        raise ScenarioError("Restore destination exists and is not a directory")
    if target.exists() and backup is None:
        raise ScenarioError("Save destination already exists; provide a new explicit backup_dir to preserve it")
    if backup is not None:
        if os.path.lexists(backup):
            raise ScenarioError("Backup directory already exists; choose a new path")
        if _overlap(backup, target) or _overlap(backup, snapshot) or backup == game:
            raise ScenarioError("Backup path overlaps the save, snapshot, or game root")
    original_inventory = _inventory(target) if target.exists() else None
    selected_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".dfeval-restore-", dir=selected_root))
    moved_original = False
    try:
        shutil.copytree(snapshot / "save", stage, symlinks=True, dirs_exist_ok=True)
        if not _matches(manifest, _inventory(stage)):
            raise ScenarioError("Staged restore failed its inventory/hash verification")
        verify_snapshot(snapshot)
        if _hash(_path(snapshot / "manifest.json")) != snapshot_hash:
            raise ScenarioError("Snapshot manifest changed during restore staging")
        _require_stopped(game_stopped)
        if original_inventory is not None:
            if _inventory(target) != original_inventory:
                raise ScenarioError("Existing save changed during staging; refusing replacement")
            assert backup is not None
            backup.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(backup):
                raise ScenarioError("Backup path appeared during staging; refusing replacement")
            try:
                os.rename(target, backup)
            except OSError as exc:
                raise ScenarioError("Cannot preserve existing save by atomic rename; choose a new backup path on the same filesystem") from exc
            moved_original = True
        elif os.path.lexists(target):
            raise ScenarioError("Save destination appeared during staging; refusing replacement")
        try:
            os.rename(stage, target)
        except OSError as exc:
            if moved_original:
                try:
                    os.rename(backup, target)
                    moved_original = False
                except OSError as rollback:
                    raise ScenarioError(f"Restore promotion and rollback failed; original save is preserved at {backup}: {rollback}") from exc
            raise ScenarioError("Restore promotion failed; original save was preserved") from exc
    finally:
        _cleanup_stage(stage, selected_root, ".dfeval-restore-")
    return {"schema_version": 1, "kind": "dfeval_local_restore_record", "save_name": name,
            "snapshot_directory": str(snapshot), "save_directory": str(target),
            "save_root": str(selected_root),
            "backup_directory": str(backup) if moved_original else None,
            "snapshot_manifest_sha256": snapshot_hash,
            "restored_files": manifest["file_count"], "restored_bytes": manifest["total_bytes"],
            "df_version": manifest["df_version"], "integrity_verified": True,
            "game_stopped_attested": True, "process_check": "No matching game process at preflight; not an OS launch lock"}
