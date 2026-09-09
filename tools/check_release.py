"""Check publication candidates without extracting archives or uploading files.

The allowlist is deliberately project-specific. This checks shipped files and,
optionally, Git-tracked working-tree files. It is not a scan of Git history or
a proof that every possible credential format has been detected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
from typing import Any, Callable
import zipfile
import zlib


MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 5000

ROOT_FILES = frozenset({
    "README.md", "PHILOSOPHY.md", "PROJECT_DIRECTION.md", "HARDWARE.md",
    "LIVE_GAME.md", "EVALUATION.md", "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md", "LICENSE",
    "pyproject.toml", "MANIFEST.in", "setup.cfg", "PKG-INFO",
    ".gitignore", ".gitattributes", "start.py",
})
TOOL_FILES = frozenset({
    "tools/dfeval.py", "tools/check_release.py", "tools/check_install.py",
    "tools/install-dfhack.ps1", "tools/start-game.ps1",
})
GITHUB_FILES = frozenset({".github/workflows/ci.yml", ".github/dependabot.yml"})
DOC_FILES = frozenset({
    "docs/model-connections.md", "docs/fixture-setup.md", "docs/benchmarking.md",
    "docs/native-loop-proof.md", "docs/native-care-pilot.md", "docs/native-care-season.md",
    "docs/assets/native-care-season.svg", "docs/data/native-care-season.json",
    "docs/data/native-care-season.csv",
})
EGG_INFO_FILES = frozenset({
    "PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
    "requires.txt", "top_level.txt",
})
DIST_INFO_FILES = frozenset({
    "METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt",
    "LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "NOTICE.txt",
    "licenses/LICENSE", "licenses/LICENSE.txt", "licenses/LICENSE.md",
    "licenses/THIRD_PARTY_NOTICES.md",
})
BLOCKED_COMPONENTS = frozenset({
    "dwarffortressitself", "dwarf fortress", "dwarf_fortress", "hack",
    "dfhack-config", "runs", "save", "saves", "runtime", "runtimes",
    ".aws", ".ssh", ".azure", ".codex", ".agents", ".git",
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    "build", "dist", ".dfeval-venv",
})
BINARY_SUFFIXES = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".pyo",
    ".bin", ".sav", ".dat", ".7z", ".zip", ".whl", ".gz", ".tar",
})
HOME_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+|/(?:Users|home)/)"
    r"([^\\/\r\n\t \"'<>]+)"
)
PLACEHOLDER_USERS = frozenset({"user", "username", "yourname", "your_username", "you", "example", "runner"})
SECRET_PATTERNS = (
    ("private-key block", re.compile(
        r"(?m)^-----BEGIN (?:(?:RSA|DSA|EC|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----\r?\n"
        r"(?:[A-Za-z0-9+/=]{16,}\r?\n)+"
        r"-----END (?:(?:RSA|DSA|EC|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----"
    )),
    ("API-token-shaped credential", re.compile(r"\bsk-(?:proj-|svcacct-|ant-(?:api\d+-)?)?[A-Za-z0-9_-]{32,}\b")),
    ("GitHub-token-shaped credential", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{32,255}|github_pat_[A-Za-z0-9_]{40,255})\b")),
)


def _safe_parts(name: str) -> list[str]:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ValueError("unsafe archive path (absolute, empty, NUL, or backslash)")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError("unsafe archive path (traversal, drive, or noncanonical component)")
    return parts


def _blocked_reason(parts: list[str]) -> str | None:
    lowered = [part.casefold() for part in parts]
    if any(part in BLOCKED_COMPONENTS for part in lowered):
        return "game/runtime, save/run output, cache, or private configuration path is excluded"
    name = lowered[-1] if lowered else ""
    if any(part.startswith(".env") for part in lowered) or name in {
        "credentials", "credentials.json", "token.json", "tokens.json", "id_rsa", "id_ed25519",
    } or name.endswith((".pem", ".key")) or name.startswith("service_account"):
        return "credential/environment filename is excluded"
    if Path(name).suffix in BINARY_SUFFIXES:
        return "binary/runtime/archive/save payload is excluded"
    return None


def _package_source(parts: list[str]) -> bool:
    if len(parts) < 2 or parts[0] != "dfeval":
        return False
    if parts[-1].endswith(".py"):
        return all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", component) for component in parts[1:-1]) and bool(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", parts[-1])
        )
    if len(parts) == 3 and parts[1] == "static":
        return parts[2] in {"index.html", "style.css", "app.js", "LICENSE.txt"}
    if len(parts) == 3 and parts[:2] == ["dfeval", "examples"] and parts[2] in {"native_probe.json", "native_model.json"}:
        return True
    return len(parts) == 4 and parts[1:3] == ["bridge", "lua"] and parts[-1] in {
        "dfeval-live.lua", "dfeval-bridge.lua", "dfeval-prepare.lua",
    }


def _allowed(parts: list[str], kind: str) -> bool:
    path = "/".join(parts)
    if kind == "wheel":
        if _package_source(parts):
            return True
        return len(parts) >= 2 and bool(re.fullmatch(r"dfeval-[0-9][A-Za-z0-9.!+_-]*\.dist-info", parts[0])) and "/".join(parts[1:]) in DIST_INFO_FILES
    if path in ROOT_FILES or path in TOOL_FILES or path in GITHUB_FILES or path in DOC_FILES:
        return True
    if parts[:1] == ["src"] and _package_source(parts[1:]):
        return True
    if kind == "sdist" and len(parts) == 3 and parts[:2] == ["src", "dfeval.egg-info"] and parts[2] in EGG_INFO_FILES:
        return True
    if len(parts) == 2 and parts[0] == "tests" and re.fullmatch(r"(?:test_[A-Za-z0-9_]+|conftest)\.py", parts[1]):
        return True
    if path == "tests/fixtures/native-loop-initial-observations.json":
        return True
    if len(parts) == 2 and parts[0] == "scenarios" and re.fullmatch(r"[A-Za-z0-9_-]+\.json", parts[1]):
        return True
    return kind == "source" and path == "runs/.gitkeep"


def _content_problems(data: bytes) -> list[str]:
    if data.startswith((b"MZ", b"\x7fELF", b"\xfe\xed\xfa", b"\xcf\xfa\xed\xfe", b"PK\x03\x04", b"\x1f\x8b")) or b"\x00" in data:
        return ["binary payload disguised as an allowed text file"]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ["non-UTF-8/binary payload is not permitted"]
    problems = [label + " detected (value withheld)" for label, pattern in SECRET_PATTERNS if pattern.search(text)]
    if any(match.group(1).casefold() not in PLACEHOLDER_USERS for match in HOME_PATH.finditer(text)) or re.search(r"(?<![A-Za-z0-9_])/" + "root/", text):
        problems.append("absolute personal home path detected; use a portable path or placeholder")
    return problems


def _inspect_archive(path: Path, kind: str, issue: Callable[..., None]) -> dict[str, Any]:
    checked = 0
    total_bytes = 0
    members = 0
    seen: set[str] = set()
    sdist_root: str | None = None

    def inspect(name: str, is_directory: bool, regular: bool, size: int, read: Callable[[], bytes]) -> None:
        nonlocal checked, total_bytes, members, sdist_root
        members += 1
        if members > MAX_MEMBERS:
            raise ValueError(f"archive exceeds {MAX_MEMBERS} members")
        # Bound every member, including rejected paths: skipping an oversized
        # member in a compressed tar can itself require decompressing it.
        total_bytes += size
        if size < 0 or size > MAX_MEMBER_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("archive exceeds bounded text-content inspection size")
        try:
            parts = _safe_parts(name)
        except ValueError as exc:
            issue(path.name, name, str(exc))
            return
        canonical = "/".join(parts).casefold()
        if canonical in seen:
            issue(path.name, name, "duplicate or case-colliding archive member")
            return
        seen.add(canonical)
        if kind == "sdist":
            root, *parts = parts
            if not re.fullmatch(r"dfeval-[0-9][A-Za-z0-9.!+_-]*", root):
                issue(path.name, name, "sdist must have one dfeval-version top-level directory")
                return
            if sdist_root is not None and root != sdist_root:
                issue(path.name, name, "sdist contains multiple top-level directories")
                return
            sdist_root = root
        if not regular and not is_directory:
            issue(path.name, name, "links and special archive members are not permitted")
            return
        blocked = _blocked_reason(parts)
        if blocked:
            issue(path.name, name, blocked)
            return
        if is_directory:
            return
        if not parts or not _allowed(parts, kind):
            issue(path.name, name, "unexpected file outside the publication allowlist")
            return
        data = read()
        if len(data) > MAX_MEMBER_BYTES:
            raise ValueError("member exceeds bounded text-content inspection size")
        checked += 1
        for problem in _content_problems(data):
            issue(path.name, name, problem)

    try:
        if path.stat().st_size > MAX_TOTAL_BYTES:
            raise ValueError("archive exceeds bounded inspection size")
        if kind == "wheel" or path.name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)

                    def read_zip(member=member) -> bytes:
                        with archive.open(member) as stream:
                            return stream.read(MAX_MEMBER_BYTES + 1)

                    # ZipInfo normalizes backslashes on Windows. Check the raw
                    # central-directory spelling before that normalization.
                    inspect(member.orig_filename, member.is_dir() and file_type in (0, stat.S_IFDIR),
                            file_type in (0, stat.S_IFREG), member.file_size, read_zip)
        else:
            with tarfile.open(path, mode="r:*") as archive:
                for member in archive:

                    def read_tar(member=member) -> bytes:
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ValueError("regular archive member cannot be read")
                        with stream:
                            return stream.read(MAX_MEMBER_BYTES + 1)

                    inspect(member.name, member.isdir(), member.isfile(), member.size, read_tar)
        if checked == 0:
            issue(path.name, None, "no allowed source or metadata files were inspected")
    except (OSError, ValueError, EOFError, zipfile.BadZipFile, tarfile.TarError, RuntimeError, zlib.error) as exc:
        issue(path.name, None, f"archive inspection failed: {exc}")
    return {"name": path.name, "kind": kind, "members": members, "files_checked": checked, "declared_bytes": total_bytes}


def _inspect_source(root: Path, issue: Callable[..., None]) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(root), "status": "skipped", "files_checked": 0,
                              "scope": "Git-tracked working-tree files; not Git history or staged blob contents"}
    if not (root / ".git").exists():
        result["reason"] = "No Git repository initialized at this path"
        return result
    try:
        process = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"],
                                 capture_output=True, check=False, timeout=20, shell=False)
    except FileNotFoundError:
        result["reason"] = "Git executable unavailable"
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        issue("source", None, f"Git file inventory failed: {exc}")
        result["status"] = "failed"
        return result
    if process.returncode:
        issue("source", None, "git ls-files failed")
        result["status"] = "failed"
        return result
    result["status"] = "checked"
    filenames = process.stdout.decode("utf-8", errors="surrogateescape").split("\x00")
    for name in filter(None, filenames):
        try:
            parts = _safe_parts(name)
            # The intentionally empty run-directory marker is the sole source
            # exception; it is never permitted in a distribution.
            blocked = _blocked_reason(parts) if name != "runs/.gitkeep" else None
            if blocked or not _allowed(parts, "source"):
                issue("source", name, blocked or "unexpected tracked file outside the publication allowlist")
                continue
            path = root.joinpath(*parts)
            if path.is_symlink() or not path.is_file():
                issue("source", name, "tracked candidate must be an existing regular file, not a link")
                continue
            if path.stat().st_size > MAX_MEMBER_BYTES:
                issue("source", name, "tracked file exceeds bounded text-content inspection size")
                continue
            data = path.read_bytes()
            if name == "runs/.gitkeep" and data:
                issue("source", name, "run-directory marker must be empty")
            for problem in _content_problems(data):
                issue("source", name, problem)
            result["files_checked"] += 1
        except (OSError, ValueError) as exc:
            issue("source", name, f"source inspection failed: {exc}")
    if not any(filenames):
        result.update(status="skipped", reason="Git has no tracked files yet")
    return result


def check_release(distribution_dir: str | Path, source_root: str | Path | None = None) -> dict[str, Any]:
    """Return a JSON-ready CI report; no archive content is extracted or run."""
    directory = Path(distribution_dir).expanduser().resolve()
    issues: list[dict[str, Any]] = []
    archives = []

    def issue(artifact: str, member: str | None, reason: str) -> None:
        issues.append({"artifact": artifact, "member": member, "reason": reason})

    if not directory.is_dir():
        issue("release", None, "distribution directory does not exist")
    else:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                issue(path.name, None, "release directory may contain only regular distribution files")
                continue
            if path.name.endswith(".whl"):
                kind = "wheel"
            elif path.name.endswith((".tar.gz", ".tgz", ".zip")):
                kind = "sdist"
            else:
                issue(path.name, None, "unexpected release file; expected a wheel or source distribution")
                continue
            archives.append(_inspect_archive(path, kind, issue))
        if not archives:
            issue("release", None, "no wheel or source distribution found")
    source = _inspect_source(Path(source_root).expanduser().resolve(), issue) if source_root is not None else None
    return {"ok": not issues, "directory": str(directory), "archives": archives,
            "source": source, "issues": issues}


def format_report(report: dict[str, Any]) -> str:
    inspected = sum(archive["files_checked"] for archive in report["archives"])
    lines = [f"{'PASS' if report['ok'] else 'FAIL'}: {len(report['archives'])} distributions; {inspected} archived files checked."]
    if report["source"]:
        source = report["source"]
        lines.append(f"Source: {source['status']}; {source['files_checked']} tracked working-tree files checked."
                     + (f" {source['reason']}." if source.get("reason") else ""))
    for issue in report["issues"]:
        location = issue["artifact"] + (f"!{issue['member']}" if issue["member"] else "")
        lines.append(f"ERROR {location}: {issue['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution_dir", nargs="?", default="dist")
    parser.add_argument("--source-root", help="Also inspect Git-tracked working-tree files at this repository root")
    parser.add_argument("--json", action="store_true", help="Print the complete report as JSON")
    args = parser.parse_args(argv)
    report = check_release(args.distribution_dir, args.source_root)
    print(json.dumps(report, indent=2, ensure_ascii=True) if args.json else format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
