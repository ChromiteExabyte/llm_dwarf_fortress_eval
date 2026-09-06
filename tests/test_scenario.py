"""Snapshot byte integrity and preservation, using tiny fake save files only."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dfeval import scenario
from dfeval.scenario import ScenarioError, restore_save, snapshot_save, verify_snapshot

_native_process_check = scenario._running_game_processes


@pytest.fixture(autouse=True)
def stopped_processes(monkeypatch):
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: [])


def test_explicit_appdata_save_root_capture_restore_without_portable_roots(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    (game / "release notes.txt").write_text("Release notes for 53.16\n")
    appdata = tmp_path / "AppData" / "Roaming"
    save_root = appdata / "Bay 12 Games" / "Dwarf Fortress" / "save"
    saved = save_root / "region1"
    saved.mkdir(parents=True)
    (saved / "world.sav").write_bytes(b"native-layout fake world")
    monkeypatch.setenv("APPDATA", str(appdata))
    snapshot = tmp_path / "snapshot"
    with pytest.raises(ScenarioError, match="--save-root"):
        snapshot_save(game, "region1", snapshot, game_stopped=True)
    manifest = snapshot_save(game, "region1", snapshot, game_stopped=True, save_root=save_root)
    assert set(manifest) == scenario._MANIFEST_KEYS  # Snapshot format remains portable and unchanged.
    assert str(save_root) not in json.dumps(manifest)
    (saved / "world.sav").write_bytes(b"post-run world")
    backup = tmp_path / "backup"
    receipt = restore_save(snapshot, game, backup_dir=backup, game_stopped=True, save_root=save_root)
    assert (saved / "world.sav").read_bytes() == b"native-layout fake world"
    assert (backup / "world.sav").read_bytes() == b"post-run world"
    assert receipt["save_root"] == str(save_root)
    assert receipt["save_directory"] == str(saved)
    assert not (game / "save").exists() and not (game / "data" / "save").exists()


def test_explicit_save_root_overrides_same_named_portable_save(tmp_path):
    game, original = make_game(tmp_path)
    external_root = tmp_path / "external saves"
    external_save = external_root / "region1"
    external_save.mkdir(parents=True)
    (external_save / "world.sav").write_bytes(b"chosen external save")
    snapshot = tmp_path / "snapshot"
    snapshot_save(game, "region1", snapshot, game_stopped=True, save_root=external_root)
    assert (snapshot / "save" / "world.sav").read_bytes() == b"chosen external save"
    assert (original / "world.sav").read_bytes() == b"fake saved world\x00\xff"


def test_explicit_root_must_exist_and_snapshot_must_stay_outside_it(tmp_path):
    game, source = make_game(tmp_path)
    with pytest.raises(ScenarioError, match="existing directory"):
        snapshot_save(game, "region1", tmp_path / "snapshot", game_stopped=True, save_root=tmp_path / "missing")
    with pytest.raises(ScenarioError, match="outside"):
        snapshot_save(game, "region1", source.parent / "snapshot", game_stopped=True, save_root=source.parent)
    with pytest.raises(ScenarioError, match="nonempty"):
        snapshot_save(game, "region1", tmp_path / "snapshot", game_stopped=True, save_root="")


@pytest.mark.parametrize("first,second,expected", [
    ("53.16", "v0.53.16 win64 ITCH", True),
    ("v0.53.16 win64 ITCH", "53.16", True),
    ("53.16", "v0.53.16 linux64 STEAM", True),
    ("53.17", "v0.53.16 win64 ITCH", False),
    ("53.16", "v0.53.17 win64 ITCH", False),
    ("53.16", "v0.53.160 win64 ITCH", False),
    ("53.16", "53.16-r1.1", False),
    ("53.16", "53.16 beta", False),
    (None, "v0.53.16 win64 ITCH", False),
    ("test-df", "test-df", True),
])
def test_cross_source_game_version_compatibility_is_narrow(first, second, expected):
    assert scenario.compatible_df_versions(first, second) is expected


def make_game(tmp_path, layout="save"):
    game = tmp_path / "Game 矮人 with spaces"
    save = game / layout / "region1"
    (save / "nested" / "empty").mkdir(parents=True)
    (save / "world.sav").write_bytes(b"fake saved world\x00\xff")
    (save / "nested" / "units.dat").write_bytes(b"fake dwarf units")
    (game / "release notes.txt").write_text("Release notes for 53.16\n", encoding="utf-8")
    return game, save


def capture(tmp_path, layout="save"):
    game, save = make_game(tmp_path, layout)
    snapshot = tmp_path / "Snapshot 矮人 baseline"
    manifest = snapshot_save(game, "region1", snapshot, game_stopped=True)
    return game, save, snapshot, manifest


@pytest.mark.parametrize("layout", ["save", "data/save"])
def test_capture_verifies_complete_save_and_manifest_is_portable(tmp_path, layout):
    game, source, snapshot, manifest = capture(tmp_path, layout)
    assert manifest == verify_snapshot(snapshot)
    assert manifest["df_version"] == "53.16"
    assert manifest["file_count"] == 2
    assert manifest["directories"] == ["nested", "nested/empty"]
    assert manifest["total_bytes"] == sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
    encoded = (snapshot / "manifest.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in encoded
    assert all(not Path(item["path"]).is_absolute() for item in manifest["files"])
    assert (source / "world.sav").read_bytes() == (snapshot / "save" / "world.sav").read_bytes()


@pytest.mark.parametrize("name", ["", ".", "..", "../region1", "a/b", "a\\b", "C:region1", "/absolute", "region1.", "region1 "])
def test_save_name_traversal_rejected_before_copy(tmp_path, name):
    game, _ = make_game(tmp_path)
    with pytest.raises(ScenarioError, match="basename"):
        snapshot_save(game, name, tmp_path / "snapshot", game_stopped=True)
    assert not (tmp_path / "snapshot").exists()


def test_capture_and_restore_require_explicit_stopped_attestation(tmp_path):
    game, _, snapshot, _ = capture(tmp_path)
    with pytest.raises(ScenarioError, match="game_stopped"):
        snapshot_save(game, "region1", tmp_path / "second")
    with pytest.raises(ScenarioError, match="game_stopped"):
        restore_save(snapshot, game, "replay")


def test_detected_running_game_blocks_attested_operation(tmp_path, monkeypatch):
    game, _, snapshot, _ = capture(tmp_path)
    monkeypatch.setattr(scenario, "_running_game_processes", lambda: ["Dwarf Fortress.exe"])
    with pytest.raises(ScenarioError, match="still running"):
        restore_save(snapshot, game, "replay", game_stopped=True)
    assert not (game / "save" / "replay").exists()


@pytest.mark.parametrize("corruption", ["bytes", "missing", "extra", "empty_directory"])
def test_corruption_blocks_verification_and_restore(tmp_path, corruption):
    game, _, snapshot, _ = capture(tmp_path)
    if corruption == "bytes":
        (snapshot / "save" / "world.sav").write_bytes(b"modified world")
    elif corruption == "missing":
        (snapshot / "save" / "world.sav").unlink()
    elif corruption == "extra":
        (snapshot / "save" / "unexpected.dat").write_bytes(b"unlisted")
    else:
        (snapshot / "save" / "unexpected empty directory").mkdir()
    with pytest.raises(ScenarioError, match="integrity"):
        verify_snapshot(snapshot)
    with pytest.raises(ScenarioError, match="integrity"):
        restore_save(snapshot, game, "replay", game_stopped=True)
    assert not (game / "save" / "replay").exists()


@pytest.mark.parametrize("bad_path", ["../../outside", "/absolute", "C:/outside", "nested\\outside"])
def test_manifest_traversal_is_rejected(tmp_path, bad_path):
    _, _, snapshot, manifest = capture(tmp_path)
    manifest["files"][0]["path"] = bad_path
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ScenarioError):
        verify_snapshot(snapshot)


def test_symlink_save_content_cannot_be_captured(tmp_path):
    game, save = make_game(tmp_path)
    outside = tmp_path / "outside-secret.dat"
    outside.write_bytes(b"must never be copied")
    try:
        (save / "linked.dat").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Host cannot create test symlinks")
    with pytest.raises(ScenarioError, match="symlink|reparse"):
        snapshot_save(game, "region1", tmp_path / "snapshot", game_stopped=True)
    assert not (tmp_path / "snapshot").exists()


def test_symlink_snapshot_payload_is_rejected(tmp_path):
    _, _, snapshot, _ = capture(tmp_path)
    world = snapshot / "save" / "world.sav"
    external = tmp_path / "external-world.sav"
    external.write_bytes(world.read_bytes())
    world.unlink()
    try:
        world.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("Host cannot create test symlinks")
    with pytest.raises(ScenarioError, match="symlink|reparse"):
        verify_snapshot(snapshot)


def test_repeatable_restore_preserves_snapshot_and_backs_up_existing_save(tmp_path):
    game, original, snapshot, manifest = capture(tmp_path)
    snapshot_manifest = (snapshot / "manifest.json").read_bytes()
    first = restore_save(snapshot, game, "replay", game_stopped=True)
    replay = Path(first["save_directory"])
    assert first["backup_directory"] is None
    assert scenario._inventory(replay) == scenario._inventory(original)
    changed = b"progress made after starting the baseline"
    (replay / "world.sav").write_bytes(changed)
    with pytest.raises(ScenarioError, match="already exists"):
        restore_save(snapshot, game, "replay", game_stopped=True)
    backup = tmp_path / "saved progress backup"
    second = restore_save(snapshot, game, "replay", backup, game_stopped=True)
    assert second["backup_directory"] == str(backup)
    assert (backup / "world.sav").read_bytes() == changed
    assert scenario._inventory(replay) == scenario._inventory(original)
    assert (snapshot / "manifest.json").read_bytes() == snapshot_manifest
    assert verify_snapshot(snapshot) == manifest


def test_failed_copy_leaves_original_and_backup_paths_untouched(tmp_path, monkeypatch):
    game, original, snapshot, _ = capture(tmp_path)
    before = scenario._inventory(original)
    backup = tmp_path / "backup"

    def fail_copy(*args, **kwargs):
        raise OSError("injected disk-full error")

    monkeypatch.setattr(scenario.shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="disk-full"):
        restore_save(snapshot, game, "region1", backup, game_stopped=True)
    assert scenario._inventory(original) == before
    assert not backup.exists()
    assert not list(original.parent.glob(".dfeval-restore-*"))


def test_failed_promotion_rolls_back_original_without_deleting_it(tmp_path, monkeypatch):
    game, original, snapshot, _ = capture(tmp_path)
    (original / "world.sav").write_bytes(b"original progress that must survive")
    before = scenario._inventory(original)
    backup = tmp_path / "backup"
    rename = scenario.os.rename

    def fail_stage_promotion(source, destination):
        if Path(source).name.startswith(".dfeval-restore-"):
            raise OSError("injected promotion failure")
        return rename(source, destination)

    monkeypatch.setattr(scenario.os, "rename", fail_stage_promotion)
    with pytest.raises(ScenarioError, match="promotion failed"):
        restore_save(snapshot, game, "region1", backup, game_stopped=True)
    assert scenario._inventory(original) == before
    assert not backup.exists()  # Original was moved back, not removed.
    assert not list(original.parent.glob(".dfeval-restore-*"))


def test_existing_backup_is_never_overwritten(tmp_path):
    game, original, snapshot, _ = capture(tmp_path)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Backup directory already exists"):
        restore_save(snapshot, game, "region1", backup, game_stopped=True)
    assert (backup / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (original / "world.sav").is_file()


def test_missing_world_and_nested_capture_destination_are_rejected(tmp_path):
    game, original = make_game(tmp_path)
    with pytest.raises(ScenarioError, match="outside"):
        snapshot_save(game, "region1", original / "nested" / "snapshot", game_stopped=True)
    (original / "world.sav").unlink()
    with pytest.raises(ScenarioError, match="world.sav"):
        snapshot_save(game, "region1", tmp_path / "snapshot", game_stopped=True)


def test_backup_outside_filesystem_failure_leaves_original_in_place(tmp_path, monkeypatch):
    game, original, snapshot, _ = capture(tmp_path)
    before = scenario._inventory(original)
    rename = scenario.os.rename

    def cross_device(source, destination):
        if Path(source) == original:
            raise OSError("cross-device link")
        return rename(source, destination)

    monkeypatch.setattr(scenario.os, "rename", cross_device)
    with pytest.raises(ScenarioError, match="same filesystem"):
        restore_save(snapshot, game, "region1", tmp_path / "backup", game_stopped=True)
    assert scenario._inventory(original) == before


def test_save_changed_during_capture_is_not_published(tmp_path, monkeypatch):
    game, original = make_game(tmp_path)
    copytree = scenario.shutil.copytree

    def copy_then_change(source, destination, *args, **kwargs):
        result = copytree(source, destination, *args, **kwargs)
        if Path(source) == original:
            (original / "world.sav").write_bytes(b"game changed while capture was in progress")
        return result

    monkeypatch.setattr(scenario.shutil, "copytree", copy_then_change)
    with pytest.raises(ScenarioError, match="changed during capture"):
        snapshot_save(game, "region1", tmp_path / "snapshot", game_stopped=True)
    assert not (tmp_path / "snapshot").exists()
    assert not list(tmp_path.glob(".dfeval-capture-*"))


def test_windows_process_check_uses_executable_names_without_game_calls(monkeypatch):
    monkeypatch.setattr(scenario.platform, "system", lambda: "Windows")
    calls = []

    def tasklist(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='"python.exe","10","Console"\n"Dwarf Fortress.exe","11","Console"\n')

    monkeypatch.setattr(scenario.subprocess, "run", tasklist)
    assert _native_process_check() == ["Dwarf Fortress.exe"]
    assert calls == [["tasklist", "/FO", "CSV", "/NH"]]


def test_process_detection_failure_does_not_count_as_stopped(monkeypatch):
    monkeypatch.setattr(scenario.platform, "system", lambda: "Windows")
    monkeypatch.setattr(scenario.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(ScenarioError, match="could not verify"):
        _native_process_check()


def test_linux_process_check_reads_native_names_from_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "456").mkdir()
    (proc / "123" / "comm").write_text("Dwarf_Fortress\n", encoding="utf-8")
    (proc / "456" / "comm").write_text("python\n", encoding="utf-8")
    monkeypatch.setattr(scenario.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scenario, "Path", lambda value: proc if value == "/proc" else Path(value))
    assert _native_process_check() == ["Dwarf_Fortress"]


def test_restore_refuses_wrong_pinned_snapshot_before_replacement(tmp_path):
    game, original, snapshot, _ = capture(tmp_path)
    before = scenario._inventory(original)
    with pytest.raises(ScenarioError, match="expected starting-save identity"):
        restore_save(snapshot, game, backup_dir=tmp_path / "backup", game_stopped=True,
                     expected_manifest_sha256="0" * 64)
    assert scenario._inventory(original) == before
    assert not (tmp_path / "backup").exists()


def test_consistent_snapshot_changed_during_staging_is_not_promoted(tmp_path, monkeypatch):
    game, original, snapshot, manifest = capture(tmp_path)
    before = scenario._inventory(original)
    expected = scenario._hash(snapshot / "manifest.json")
    copytree = scenario.shutil.copytree

    def copy_then_replace(source, destination, *args, **kwargs):
        result = copytree(source, destination, *args, **kwargs)
        if Path(source) == snapshot / "save":
            (snapshot / "save" / "world.sav").write_bytes(b"a different but internally consistent snapshot")
            manifest.update(scenario._inventory(snapshot / "save"))
            (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return result

    monkeypatch.setattr(scenario.shutil, "copytree", copy_then_replace)
    with pytest.raises(ScenarioError, match="changed during restore staging"):
        restore_save(snapshot, game, backup_dir=tmp_path / "backup", game_stopped=True,
                     expected_manifest_sha256=expected)
    assert scenario._inventory(original) == before
    assert not (tmp_path / "backup").exists()
    assert not list(original.parent.glob(".dfeval-restore-*"))
