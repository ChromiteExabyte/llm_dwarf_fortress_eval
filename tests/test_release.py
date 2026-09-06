"""Publication checks use synthetic archives, never extracted game files."""

import io
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tarfile
import zipfile

import pytest

_spec = importlib.util.spec_from_file_location("dfeval_release_check", Path(__file__).resolve().parents[1] / "tools" / "check_release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def _wheel(directory, extra=None):
    path = directory / "dfeval-0.1.0-py3-none-any.whl"
    contents = {
        "dfeval/__init__.py": b'"""A small evaluation harness."""\n',
        "dfeval/bridge/lua/dfeval-live.lua": b"-- native bridge\n",
        "dfeval-0.1.0.dist-info/METADATA": b"Name: dfeval\nVersion: 0.1.0\n",
        "dfeval-0.1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "dfeval-0.1.0.dist-info/RECORD": b"",
        "dfeval-0.1.0.dist-info/licenses/LICENSE": b"Project license text\n",
    }
    contents.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in contents.items():
            # Preserve deliberately malicious raw spelling even on Windows,
            # where ZipInfo construction normally replaces backslashes.
            info = zipfile.ZipInfo()
            info.filename = info.orig_filename = name
            archive.writestr(info, data)
    return path


def _sdist(directory, extra=None, links=None):
    path = directory / "dfeval-0.1.0.tar.gz"
    contents = {
        "src/dfeval/__init__.py": b"# source\n",
        "src/dfeval/bridge/lua/dfeval-live.lua": b"-- bridge\n",
        "src/dfeval.egg-info/PKG-INFO": b"Name: dfeval\n",
        "README.md": b"Set OPENAI_API_KEY or ANTHROPIC_API_KEY. Example: sk-your-key-here.\n",
        "LICENSE": b"Project license text\n",
        "pyproject.toml": b'[project]\nname = "dfeval"\n',
        "tools/check_release.py": b"# release checker\n",
        "tests/test_release.py": b"# tests\n",
        "scenarios/default.json": b'{"difficulty": 1.0}\n',
        ".github/workflows/ci.yml": b"name: CI\n",
        ".github/dependabot.yml": b"version: 2\n",
    }
    contents.update(extra or {})
    with tarfile.open(path, "w:gz") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo("dfeval-0.1.0/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo("dfeval-0.1.0/" + name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return path


def test_clean_wheel_and_sdist_pass_and_api_key_placeholders_are_allowed(tmp_path):
    _wheel(tmp_path)
    _sdist(tmp_path)

    report = release.check_release(tmp_path)

    assert report["ok"] is True
    assert len(report["archives"]) == 2
    assert report["issues"] == []
    assert release.format_report(report).startswith("PASS:")


@pytest.mark.parametrize("name", [
    "dfeval/Dwarf Fortress.exe", "dfeval/dfhack.dll", "dfeval/runs/run.json",
    "dfeval/save/region1/world.sav", "dfeval/.env", "dfeval/.env.production",
    "dfeval/credentials.json", "dfeval/unexpected.txt",
    "../escape.py", "/outside.py", "C:/outside.py", "dfeval/../outside.py",
    "dfeval\\escape.py",
])
def test_wheel_rejects_game_runtime_runs_credentials_unexpected_files_and_traversal(tmp_path, name):
    _wheel(tmp_path, {name: b"not for publication"})

    report = release.check_release(tmp_path)

    assert report["ok"] is False
    assert any(issue["member"] == name for issue in report["issues"])
    assert not (tmp_path.parent / "escape.py").exists()


@pytest.mark.parametrize("name", ["dwarfFortressItself/Dwarf Fortress.exe", "runs/live/result.json", ".env", "../escape.py"])
def test_sdist_does_not_inherit_source_exceptions_for_game_or_run_files(tmp_path, name):
    _sdist(tmp_path, {name: b"private"})
    assert release.check_release(tmp_path)["ok"] is False


def test_binary_renamed_to_python_is_rejected(tmp_path):
    _wheel(tmp_path, {"dfeval/payload.py": b"MZ\x00runtime payload"})
    report = release.check_release(tmp_path)
    assert report["ok"] is False
    assert any("binary payload" in issue["reason"] for issue in report["issues"])


def test_size_bound_applies_even_to_members_that_would_be_rejected_by_path(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "MAX_MEMBER_BYTES", 512)
    _sdist(tmp_path, {"../oversized.bin": b"x" * 1024})

    report = release.check_release(tmp_path)

    assert report["ok"] is False
    assert any("bounded" in issue["reason"] for issue in report["issues"])


def test_private_key_api_token_and_personal_home_path_are_rejected_without_echoing_secrets(tmp_path):
    private_key = "-----BEGIN " + "PRIVATE KEY-----\n" + "aBcDeF0123456789+/=" * 4 + "\n-----END " + "PRIVATE KEY-----\n"
    token = "sk-proj-" + "abcdef0123456789" * 5
    home_path = "C:" + chr(92) + "Users" + chr(92) + "ARealPerson" + chr(92) + "private"
    _sdist(tmp_path, {"README.md": (private_key + token + "\n" + home_path).encode()})

    report = release.check_release(tmp_path)
    rendered = release.format_report(report)

    assert report["ok"] is False
    assert any("private-key" in issue["reason"] for issue in report["issues"])
    assert any("API-token" in issue["reason"] for issue in report["issues"])
    assert any("home path" in issue["reason"] for issue in report["issues"])
    assert token not in rendered
    assert home_path not in rendered


def test_tar_and_zip_links_are_rejected_without_following_them(tmp_path):
    _sdist(tmp_path, links={"src/dfeval/leak.py": "../../private.py"})
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        link = zipfile.ZipInfo("dfeval/leak.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../private.py")

    report = release.check_release(tmp_path)

    assert report["ok"] is False
    assert sum("links and special" in issue["reason"] for issue in report["issues"]) == 2


def test_duplicate_case_colliding_members_are_rejected(tmp_path):
    _wheel(tmp_path, {"dfeval/Example.py": b"# first", "dfeval/example.py": b"# second"})
    assert any("case-colliding" in issue["reason"] for issue in release.check_release(tmp_path)["issues"])


def test_corrupt_archive_and_empty_release_directory_fail(tmp_path):
    assert release.check_release(tmp_path)["ok"] is False
    (tmp_path / "dfeval-0.1.0-py3-none-any.whl").write_bytes(b"not a zip")
    report = release.check_release(tmp_path)
    assert report["ok"] is False
    assert any("inspection failed" in issue["reason"] for issue in report["issues"])


def test_missing_git_skips_source_inventory_without_skipping_archive_checks(tmp_path):
    distribution = tmp_path / "dist"
    distribution.mkdir()
    _wheel(distribution)

    report = release.check_release(distribution, source_root=tmp_path)

    assert report["ok"] is True
    assert report["source"]["status"] == "skipped"
    assert report["archives"][0]["files_checked"] > 0


def test_tracked_working_tree_candidates_are_checked_without_running_a_shell(tmp_path, monkeypatch):
    distribution = tmp_path / "dist"
    distribution.mkdir()
    _wheel(distribution)
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("Public documentation")
    (tmp_path / ".env").write_text("PRIVATE=value")
    calls = []

    def fake_git(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, b"README.md\x00.env\x00", b"")

    monkeypatch.setattr(release.subprocess, "run", fake_git)
    report = release.check_release(distribution, source_root=tmp_path)

    assert report["ok"] is False
    assert calls[0][1]["shell"] is False
    assert any(issue["artifact"] == "source" and issue["member"] == ".env" for issue in report["issues"])


def test_checker_and_test_source_do_not_trigger_their_own_secret_detection(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    _sdist(tmp_path, {
        "tools/check_release.py": (repo / "tools" / "check_release.py").read_bytes(),
        "tests/test_release.py": Path(__file__).read_bytes(),
    })
    report = release.check_release(tmp_path)
    assert report["ok"] is True, report["issues"]


def test_ci_json_report_and_exit_code(tmp_path, capsys):
    _wheel(tmp_path)
    assert release.main([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
