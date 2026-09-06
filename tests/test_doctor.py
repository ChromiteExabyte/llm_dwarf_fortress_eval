"""Offline evidence must never be mistaken for a running, playable fortress."""

import json

import pytest

from dfeval.doctor import format_report, inspect_installation, report_json


@pytest.fixture(autouse=True)
def windows_fixture_layout(monkeypatch):
    """Most fixtures are Windows layouts; inspect them consistently on either CI OS."""
    monkeypatch.setattr("dfeval.live.platform.system", lambda: "Windows")


def _file(root, relative, text=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_installation_is_reported_without_creating_it(tmp_path):
    root = tmp_path / "missing"
    report = inspect_installation(root)

    assert not root.exists()
    assert report["game"]["executable"] is None
    assert report["game"]["version"] is None
    assert report["saves"] == []
    assert report["readiness"]["ready_for_live_probe"] is False
    assert report["readiness"]["connected"] is None


def test_vanilla_version_comes_from_first_release_heading(tmp_path):
    _file(tmp_path, "Dwarf Fortress.exe")
    _file(tmp_path, "release notes.txt", "Compatible with 0.50.01+.\n\n"
          "Release notes for 53.16 (August 5, 2026):\nNew release.\n"
          "Release notes for 53.15 (June 25, 2026):\nOld release.\n")

    report = inspect_installation(tmp_path)

    assert report["game"]["version"] == "53.16"
    assert report["readiness"]["game_files_present"] is True
    assert report["dfhack"]["present"] is False
    assert report["readiness"]["ready_for_live_probe"] is False


def test_bridge_alone_does_not_establish_dfhack_installation(tmp_path):
    _file(tmp_path, "Dwarf Fortress.exe")
    _file(tmp_path, "hack/scripts/dfeval-bridge.lua", "-- a bridge script")

    report = inspect_installation(tmp_path)

    assert report["bridge"]["installed"] is True
    assert report["dfhack"]["present"] is True
    assert report["readiness"]["dfhack_files_present"] is False
    assert report["readiness"]["ready_for_live_probe"] is False


def test_installed_files_and_stale_reply_never_claim_live_readiness(tmp_path):
    _file(tmp_path, "Dwarf Fortress.exe")
    _file(tmp_path, "hack/dfhack.dll")
    _file(tmp_path, "hack/dfhack-run.exe")
    _file(tmp_path, "hack/dfhack-version.txt", "DFHack 53.16-r1\n")
    _file(tmp_path, "hack/scripts/dfeval-live.lua")
    _file(tmp_path, "save/region1/world.sav")
    _file(tmp_path, "dfeval/response.json", '{"ok": true, "pong": true}')

    report = inspect_installation(tmp_path)

    assert report["dfhack"]["version"] == "53.16-r1"
    assert report["dfhack"]["compatibility_verified"] is False
    assert report["readiness"]["ready_for_live_probe"] is True
    assert report["readiness"]["save_candidates_present"] is True
    for key in ("connected", "fortress_loaded", "playable"):
        assert report["readiness"][key] is None
    assert report["saves"][0]["fortress_verified"] is False
    assert json.loads(report_json(report)) == report
    plain = format_report(report)
    assert "Connected: unknown" in plain
    assert "Fortress loaded: unknown" in plain
    assert "Playable: unknown" in plain


def test_bundled_release_news_supplies_version_evidence(tmp_path):
    _file(tmp_path, "hack/news.rst", "DFHack 53.16-r1.1\n=================\n"
          "Fixes\n-----\nSome fixes.\n\nDFHack 53.16-r1\n===============\n")

    report = inspect_installation(tmp_path)

    assert report["dfhack"]["version"] == "53.16-r1.1"
    assert report["dfhack"]["version_evidence"] == "bundled_documentation"
    assert report["readiness"]["ready_for_live_probe"] is False


def test_legacy_bridge_cannot_satisfy_new_live_protocol_prerequisites(tmp_path):
    _file(tmp_path, "Dwarf Fortress.exe")
    _file(tmp_path, "hack/dfhack.dll")
    _file(tmp_path, "hack/scripts/dfeval-bridge.lua")

    report = inspect_installation(tmp_path)

    assert report["bridge"]["legacy_installed"] is True
    assert report["bridge"]["live_installed"] is False
    assert report["readiness"]["ready_for_live_probe"] is False


def test_save_inventory_covers_old_and_new_layout_without_guessing_contents(tmp_path):
    _file(tmp_path, "save/region2/world.sav")
    _file(tmp_path, "data/save/region1/world.sav")
    _file(tmp_path, "save/empty-folder/note.txt")
    _file(tmp_path, "save/current/world.sav")

    report = inspect_installation(tmp_path)
    saves = {save["name"]: save for save in report["saves"]}

    assert set(saves) == {"region1", "region2", "empty-folder"}
    assert saves["region1"]["has_world_save"] is True
    assert saves["region2"]["has_world_save"] is True
    assert saves["empty-folder"]["has_world_save"] is False


def test_unrecognized_version_text_stays_unknown(tmp_path):
    _file(tmp_path, "release_notes.txt", "Compatible with 0.50.01+.\n")
    _file(tmp_path, "hack/version.txt", "Copyright 2026; API version 7.2\n")

    report = inspect_installation(tmp_path)

    assert report["game"]["version"] is None
    assert report["dfhack"]["version"] is None
    assert report["dfhack"]["version_source"] is None


def test_user_script_has_priority_and_both_locations_are_reported(tmp_path):
    user_script = _file(tmp_path, "dfhack-config/scripts/dfeval-live.lua")
    bundled_script = _file(tmp_path, "hack/scripts/dfeval-live.lua")
    report = inspect_installation(tmp_path)
    assert report["bridge"]["path"] == str(user_script)
    assert report["bridge"]["live_paths"] == [str(user_script), str(bundled_script)]


@pytest.mark.parametrize("runner_relative", ["dfhack-run", "hack/dfhack-run"])
@pytest.mark.parametrize("core_relative", ["libdfhack.so", "hack/libdfhack.so"])
def test_linux_game_and_user_script_layout(tmp_path, monkeypatch, runner_relative, core_relative):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: "Linux")
    monkeypatch.setattr("dfeval.live.os.access", lambda path, mode: True)
    game = tmp_path / "Fortress 矮人 with spaces"
    executable = _file(game, "Dwarf_Fortress")
    _file(game, core_relative)
    runner = _file(game, runner_relative)
    (game / "hack").mkdir(exist_ok=True)
    _file(game, "dfhack-config/scripts/dfeval-live.lua")
    report = inspect_installation(game)
    assert report["host_platform"] == "Linux"
    assert report["game"]["executable"] == str(executable)
    assert report["dfhack"]["runner"] == str(runner)
    assert report["dfhack"]["runner_error"] is None
    assert report["readiness"]["ready_for_live_probe"] is True
    assert report["readiness"]["connected"] is None
    assert json.loads(report_json(report)) == report


def test_missing_runner_keeps_otherwise_complete_installation_unready(tmp_path):
    _file(tmp_path, "Dwarf Fortress.exe")
    _file(tmp_path, "hack/dfhack.dll")
    _file(tmp_path, "dfhack-config/scripts/dfeval-live.lua")
    report = inspect_installation(tmp_path)
    assert report["readiness"]["dfhack_files_present"] is True
    assert report["readiness"]["ready_for_live_probe"] is False
    assert report["readiness"]["host_runner_available"] is False
    assert "No Windows DFHack runner" in report["dfhack"]["runner_error"]
    assert "Install DFHack" in " ".join(report["next_steps"])


def test_unsupported_host_reports_evidence_without_claiming_native_startup(tmp_path, monkeypatch):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: "Darwin")
    _file(tmp_path, "Dwarf_Fortress")
    _file(tmp_path, "hack/libdfhack.dylib")
    _file(tmp_path, "dfhack-run")
    _file(tmp_path, "dfhack-config/scripts/dfeval-live.lua")
    report = inspect_installation(tmp_path)
    assert report["readiness"]["dfhack_files_present"] is True
    assert report["readiness"]["ready_for_live_probe"] is False
    assert "Use native Windows or Linux" in report["dfhack"]["runner_error"]
    assert "DFHack runner for Darwin: not available" in format_report(report)
