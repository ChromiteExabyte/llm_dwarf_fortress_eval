"""Local environment drift detection against fake files; no DF or model calls."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dfeval import environment
from dfeval.environment import EnvironmentCaptureError, capture_environment


def put(game, name, contents=b"configuration"):
    path = game / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "Game 矮人 with spaces"
    root.mkdir()
    return root


def test_capture_is_repeatable_portable_and_read_only(game, tmp_path):
    contents = {
        "prefs/d_init.txt": b"[TEMPERATURE:YES]",
        "data/init/init_default.txt": b"[FPS_CAP:100]",
        "hack/scripts/example.lua": b"return 1",
        "data/installed_mods/Example 矮人 (1)/objects/plants.txt": b"[PLANT:MUSHROOM]",
        "dfhack-config/control-panel.json": b'{"commands":{}}',
    }
    for name, data in contents.items():
        put(game, name, data)
    before = {path.relative_to(game).as_posix(): path.read_bytes()
              for path in game.rglob("*") if path.is_file()}
    captured = capture_environment(game)
    assert captured == capture_environment(str(game))
    assert {item["path"]: item["sha256"] for item in captured["files"]} == {
        name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    assert captured["file_count"] == len(contents)
    assert captured["total_bytes"] == sum(map(len, contents.values()))
    assert {path.relative_to(game).as_posix(): path.read_bytes()
            for path in game.rglob("*") if path.is_file()} == before
    assert str(tmp_path) not in json.dumps(captured, ensure_ascii=False)
    elsewhere = tmp_path / "Another game"
    for name, data in contents.items():
        put(elsewhere, name, data)
    assert capture_environment(elsewhere) == captured
    assert "not verified effective" in " ".join(captured["limitations"])
    assert "deterministic" in " ".join(captured["limitations"])


def test_config_change_detected_even_when_size_unchanged(game):
    path = put(game, "prefs/d_init.txt", b"[WEATHER:YES]")
    before = capture_environment(game)
    path.write_bytes(b"[WEATHER:NO ]")
    after = capture_environment(game)
    assert before["total_bytes"] == after["total_bytes"]
    assert before["fingerprint_sha256"] != after["fingerprint_sha256"]


def test_missing_present_file_and_empty_init_directory_are_distinct(game):
    missing = capture_environment(game)
    assert "prefs/init.txt" in missing["missing_roots"]
    put(game, "prefs/init.txt", b"")
    present = capture_environment(game)
    assert "prefs/init.txt" not in present["missing_roots"]
    assert present["fingerprint_sha256"] != missing["fingerprint_sha256"]
    (game / "init.d").mkdir()
    with_directory = capture_environment(game)
    assert "init.d" in with_directory["directories"]
    assert with_directory["fingerprint_sha256"] != present["fingerprint_sha256"]


def test_saves_ipc_logs_backups_models_and_generated_bridge_are_ignored(game):
    put(game, "hack/scripts/real.lua", b"return 42")
    before = capture_environment(game)
    for name in (
        "save/world/world.sav", "data/save/world/world.sav", "runs/test/events.jsonl",
        "dfhack-config/dfeval-live/session/request.json",
        "dfhack-config/dfeval-backups/id/world.sav", "models/model.bin", "stderr.log",
        "hack/scripts/logs/private.txt", "hack/scripts/backups/old.lua",
        "hack/scripts/models/model.txt", "hack/scripts/native.log",
        "hack/scripts/dfeval-live.lua", "dfhack-config/scripts/dfeval-live.lua",
    ):
        put(game, name, b"transient")
    assert capture_environment(game) == before
    put(game, "dfhack-config/scripts/custom.lua", b"this matters")
    assert capture_environment(game)["fingerprint_sha256"] != before["fingerprint_sha256"]


def test_only_declared_text_script_config_suffixes_are_hashed(game, monkeypatch):
    put(game, "mods/sample/info.txt", b"[ID:sample]")
    before = capture_environment(game)
    put(game, "mods/sample/art.png", b"x" * 100)
    monkeypatch.setattr(environment, "MAX_FILE_BYTES", 20)
    assert capture_environment(game) == before


def test_custom_internal_paths_capture_files_and_missing_declarations(game):
    put(game, "dfhack-config/script-paths.txt", b"# operator paths\n+custom scripts\n-missing scripts\n")
    put(game, "custom scripts/deep/tool.lua", b"return 12")
    captured = capture_environment(game)
    records = captured["custom_script_paths"]
    assert [(record["path"], record["status"], record["priority"]) for record in records] == [
        ("custom scripts", "captured", "before"), ("missing scripts", "missing", "after")]
    assert any(item["path"] == "custom scripts/deep/tool.lua" for item in captured["files"])
    assert "missing scripts" in captured["missing_roots"]
    put(game, "missing scripts/new.lua", b"new")
    assert capture_environment(game)["fingerprint_sha256"] != captured["fingerprint_sha256"]


def test_external_invalid_and_overbroad_custom_paths_are_explicitly_uncovered(game, tmp_path):
    external = tmp_path / "Private operator scripts"
    external.mkdir()
    put(external, "secret.lua", b"do not read")
    declarations = f"+{external}\n+../Private operator scripts\n+save\n+.\n+dfhack-config\ninvalid\n"
    put(game, "dfhack-config/script-paths.txt", declarations.encode("utf-8"))
    captured = capture_environment(game)
    assert [record["status"] for record in captured["custom_script_paths"]] == [
        "uncovered_external_path", "uncovered_traversal_path", "uncovered_excluded_path",
        "uncovered_overbroad_path", "uncovered_overbroad_path", "uncovered_invalid_declaration"]
    encoded = json.dumps(captured)
    assert str(external) not in encoded
    assert "Private operator scripts" not in encoded
    put(external, "secret.lua", b"external modification")
    assert capture_environment(game) == captured


def test_internal_absolute_custom_path_emits_only_relative_metadata(game):
    put(game, "custom/tool.lua", b"local code")
    put(game, "dfhack-config/script-paths.txt", f"+{game / 'custom'}\n".encode("utf-8"))
    captured = capture_environment(game)
    assert captured["custom_script_paths"][0]["path"] == "custom"
    assert str(game) not in json.dumps(captured)


def test_duplicate_custom_roots_do_not_double_count_files(game):
    put(game, "hack/scripts/tool.lua", b"123")
    put(game, "dfhack-config/script-paths.txt", b"+hack/scripts\n-hack/scripts\n")
    captured = capture_environment(game)
    assert captured["file_count"] == 2
    assert len([item for item in captured["files"] if item["path"].endswith("tool.lua")]) == 1


@pytest.mark.parametrize("target", ["prefs", "prefs/init.txt", "mods/mod/code.lua"])
def test_selected_reparse_point_is_rejected_without_traversal(game, monkeypatch, target):
    put(game, "prefs/init.txt", b"config")
    put(game, "mods/mod/code.lua", b"code")
    original = Path.lstat
    def marked(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == game / target:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", marked)
    with pytest.raises(EnvironmentCaptureError, match="reparse"):
        capture_environment(game)


def test_actual_symlink_is_rejected_when_host_supports_it(game, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (game / "mods").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    with pytest.raises(EnvironmentCaptureError, match="[Ss]ymlink"):
        capture_environment(game)


@pytest.mark.parametrize("limit,value,names,match", [
    ("MAX_FILE_BYTES", 3, ["prefs/init.txt"], "per-file"),
    ("MAX_TOTAL_BYTES", 7, ["prefs/init.txt", "prefs/d_init.txt"], "total"),
    ("MAX_FILES", 1, ["prefs/init.txt", "prefs/d_init.txt"], "file count"),
    ("MAX_DIRECTORIES", 1, ["mods/a/code.lua"], "directory count"),
    ("MAX_SCRIPT_PATHS_BYTES", 3, ["dfhack-config/script-paths.txt"], "per-file"),
])
def test_capture_budgets_refuse_partial_evidence(game, monkeypatch, limit, value, names, match):
    for name in names:
        put(game, name, b"1234")
    monkeypatch.setattr(environment, limit, value)
    with pytest.raises(EnvironmentCaptureError, match=match):
        capture_environment(game)


def test_custom_path_count_is_bounded(game, monkeypatch):
    put(game, "dfhack-config/script-paths.txt", b"+one\n+two\n")
    monkeypatch.setattr(environment, "MAX_CUSTOM_PATHS", 1)
    with pytest.raises(EnvironmentCaptureError, match="path count"):
        capture_environment(game)


def test_permission_failure_never_returns_partial_inventory(game, monkeypatch):
    path = put(game, "prefs/init.txt", b"config")
    original = os.open
    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError(str(path))
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(EnvironmentCaptureError, match="completely inspect") as error:
        capture_environment(game)
    assert str(game) not in str(error.value)


def test_detected_change_during_file_read_refuses_snapshot(game, monkeypatch):
    put(game, "prefs/init.txt", b"config")
    original = os.fstat
    calls = 0
    def changed(descriptor):
        nonlocal calls
        calls += 1
        info = original(descriptor)
        if calls == 2:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + 1)
        return info
    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(EnvironmentCaptureError, match="changed during"):
        capture_environment(game)


@pytest.mark.parametrize("candidate,expected", [("prefs/init.txt", "file"), ("mods", "directory")])
def test_wrong_candidate_type_fails(game, candidate, expected):
    if expected == "file":
        (game / candidate).mkdir(parents=True)
    else:
        put(game, candidate, b"not a directory")
    with pytest.raises(EnvironmentCaptureError, match=f"Expected a configuration {expected}"):
        capture_environment(game)


def test_missing_game_directory_fails(tmp_path):
    with pytest.raises(EnvironmentCaptureError, match="existing directory"):
        capture_environment(tmp_path / "missing")
