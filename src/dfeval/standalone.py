"""Export one bounded native recording as an offline, self-contained viewer.

This is a local copy of original evidence, not a public-data sanitizer. Parsing
uses a private copy of the captured bytes; source files are never modified.
Included files must still match their metadata and bytes immediately before
publication. This detects concurrent recording changes, but is not a filesystem
transaction or a claim that the experiment process has stopped.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import errno
import hashlib
import html
from importlib.resources import files
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable

from . import viewer


MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_STATIC_BYTES = 2 * 1024 * 1024
SOURCE_URL = "https://github.com/ChromiteExabyte/llm_dwarf_fortress_eval"
OMISSIONS = (
    "Game-window video and recorded frame assets are not included.",
    "Only the viewer's allowlisted evidence files are included; game saves and other files are not copied.",
)
PRIVACY_NOTICE = (
    "This local export contains original evidence, including any private prompts, "
    "model responses, and machine paths in those files. Review it before sharing."
)
STABILITY_NOTE = (
    "Included evidence matched its original file metadata and bytes at the final check. "
    "An unfinished recording may continue afterward; this export will not update."
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _inventory(root: Path) -> dict[str, tuple[int, ...]]:
    """Inspect only the same fixed names that the local viewer can download."""
    result = {}
    for name in sorted(viewer.EVIDENCE_FILES):
        try:
            (root / name).lstat()
        except FileNotFoundError:
            continue
        try:
            path = viewer._safe_file(root, name)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError()
        except OSError as exc:
            raise ValueError(f"Evidence is not a safe regular file: {name}") from exc
        result[name] = _signature(info)
    return result


def _changed() -> ValueError:
    return ValueError("Source evidence changed during export; retry when the recording has stopped writing")


def _capture(root: Path) -> tuple[dict[str, tuple[int, ...]], dict[str, bytes]]:
    inventory = _inventory(root)
    captured: dict[str, bytes] = {}
    total = 0
    for name, signature in inventory.items():
        if signature[3] > MAX_SOURCE_BYTES - total:
            raise ValueError("Source evidence exceeds the standalone export's 32 MiB total limit")
        try:
            data = viewer._read_file(root, name, MAX_SOURCE_BYTES - total)
        except OSError as exc:
            raise _changed() from exc
        captured[name] = data
        total += len(data)
    if _inventory(root) != inventory:
        raise _changed()
    return inventory, captured


def _verify_source(root: Path, identity: tuple[int, int],
                   inventory: dict[str, tuple[int, ...]], captured: dict[str, bytes]) -> None:
    info = root.stat()
    if (info.st_dev, info.st_ino) != identity or _inventory(root) != inventory:
        raise _changed()
    for name, original in captured.items():
        try:
            current = viewer._read_file(root, name, MAX_SOURCE_BYTES)
        except OSError as exc:
            raise _changed() from exc
        if current != original:
            raise _changed()
    if _inventory(root) != inventory:
        raise _changed()


def _output_path(root: Path, output_file: str | Path) -> Path:
    requested = Path(output_file).expanduser().absolute()
    if requested.suffix.lower() != ".html":
        raise ValueError("Standalone output must have an .html suffix")
    if requested.exists() or requested.is_symlink():
        raise FileExistsError("Standalone output already exists; choose a new file")
    if not requested.parent.is_dir():
        raise ValueError("The standalone output parent directory must already exist")
    output = requested.parent.resolve() / requested.name
    if output.is_relative_to(root):
        raise ValueError("Standalone output must be outside the source recording directory")
    return output


def _safe_json(value: Any) -> str:
    # A JSON script is still parsed by the HTML tokenizer. Escape characters
    # that could close it, introduce markup, or start an HTML character entity.
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _asset(name: str) -> str:
    asset = files("dfeval").joinpath("static", name)
    with asset.open("rb") as stream:
        raw = stream.read(MAX_STATIC_BYTES + 1)
    if len(raw) > MAX_STATIC_BYTES:
        raise ValueError("Packaged viewer asset exceeds the standalone size limit")
    # Normalize newlines before hashing: HTML parsers normalize CRLF to LF.
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _inline_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _document(payload: dict[str, Any]) -> bytes:
    template, css, script = (_asset(name) for name in ("index.html", "style.css", "app.js"))
    license_text = _asset("LICENSE.txt")
    if "</style" in css.lower() or "</script" in script.lower():
        raise ValueError("Packaged viewer asset contains an unsafe inline closing tag")
    stylesheet = '<link rel="stylesheet" href="/style.css">'
    external_script = '<script src="/app.js" defer></script>'
    if (template.count(stylesheet) != 1 or template.count(external_script) != 1
            or template.count("<head>") != 1 or template.count("</body>") != 1
            or template.count("</main>") != 1):
        raise ValueError("Packaged viewer template does not match the standalone embedding contract")
    csp = (
        "default-src 'none'; script-src " + _inline_hash(script) + "; "
        "style-src " + _inline_hash(css) + "; script-src-attr 'none'; style-src-attr 'none'; "
        "connect-src 'none'; img-src 'none'; media-src 'none'; font-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    policy = '<meta http-equiv="Content-Security-Policy" content="' + html.escape(csp, quote=True) + '">'
    template = template.replace("<head>", '<head>\n  ' + policy + '\n  <meta name="referrer" content="no-referrer">', 1)
    template = template.replace(stylesheet, "<style>" + css + "</style>", 1)
    template = template.replace(external_script, "", 1)
    license_notice = (
        '<details class="panel" id="standalone-viewer-license"><summary>Viewer license</summary>'
        '<p>The DFEval viewer software is licensed under GPL-3.0-or-later. '
        'This HTML contains the viewer\'s HTML, CSS, and JavaScript source. '
        '<a href="' + SOURCE_URL + '" rel="noreferrer">Project source code</a>. '
        'This notice does not assign a license to the recorded evidence.</p>'
        '<pre id="viewer-license-text" tabindex="0">' + html.escape(license_text) + '</pre></details>'
    )
    template = template.replace("</main>", license_notice + "\n</main>", 1)
    embedded = '<script type="application/json" id="embedded-recording">' + _safe_json(payload) + '</script>'
    template = template.replace("</body>", embedded + "\n<script>" + script + "</script>\n</body>", 1)
    document = template.encode("utf-8")
    if len(document) > MAX_DOCUMENT_BYTES:
        raise ValueError("Standalone HTML exceeds the 64 MiB document limit")
    return document


def _unlink_owned(path: Path, identity: tuple[int, int] | None) -> None:
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def _hard_links_unsupported(exc: OSError) -> bool:
    return (exc.errno in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EPERM}
            or getattr(exc, "winerror", None) in {1, 17, 50})


def _verify_output(output: Path, identity: tuple[int, int], document: bytes) -> None:
    """Check owned bytes after close; Windows may finalize mtime on handle close."""
    def owned(info: os.stat_result) -> bool:
        return (stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity
                and info.st_size == len(document))

    if not owned(output.lstat()):
        raise ValueError("Standalone output changed during export")
    descriptor = os.open(output, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not owned(os.fstat(stream.fileno())) or stream.read(len(document) + 1) != document:
            raise ValueError("Standalone output changed during export")
        if not owned(os.fstat(stream.fileno())):
            raise ValueError("Standalone output changed during export")
    if not owned(output.lstat()):
        raise ValueError("Standalone output changed during export")


def _write_exclusive(output: Path, document: bytes, verify: Callable[[], None]) -> None:
    """Portable fallback: success follows a complete write and a new source check.

    The new path may be visible during writing. If anything fails, remove only
    this export's file; a competing writer's replacement must remain untouched.
    """
    identity = None
    complete = False
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        verify()
        _verify_output(output, identity, document)
        complete = True
    finally:
        if not complete and identity is not None:
            _unlink_owned(output, identity)


def _publish(output: Path, document: bytes, verify: Callable[[], None]) -> str:
    """Publish exclusively, using atomic linking when the filesystem supports it."""
    temporary: Path | None = None
    identity = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".dfeval-export-", suffix=".tmp",
                                         dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        verify()
        # Linking is atomic and fails if the destination appeared concurrently.
        # FAT/exFAT may require exclusive creation, which also forbids overwrite.
        try:
            os.link(temporary, output)
        except OSError as exc:
            if not _hard_links_unsupported(exc):
                raise
            _write_exclusive(output, document, verify)
            return "exclusive_creation"
        return "atomic_exclusive_hard_link"
    finally:
        if temporary is not None:
            _unlink_owned(temporary, identity)


def export_standalone(run_dir: str | Path, output_file: str | Path) -> dict[str, Any]:
    """Copy a native experiment/probe to one new HTML file without network access.

    Partial and failed recordings retain their viewer status, parse warnings,
    and exact raw downloads. Mock/unknown recordings are rejected. The output
    parent must exist. Publication uses an atomic hard link when supported;
    otherwise an exclusively created output may be visible during writing, and
    success requires another source check after the write. The returned
    publication field identifies which mechanism succeeded. Source evidence is
    capped at 32 MiB total and final HTML at 64 MiB.
    """
    root = viewer._root_directory(run_dir)
    output = _output_path(root, output_file)
    info = root.stat()
    identity = (info.st_dev, info.st_ino)
    inventory, captured = _capture(root)
    with tempfile.TemporaryDirectory(prefix="dfeval-recording-") as directory:
        copied = Path(directory)
        for name, raw in captured.items():
            with (copied / name).open("xb") as stream:
                stream.write(raw)
        payload = viewer.load_run(copied, include_frames=False)
        if payload["origin"]["kind"] not in ("native_experiment", "live_probe"):
            raise ValueError("Standalone export requires a recognized native experiment or native game probe")
        if payload["origin"]["run_name"] == copied.name:
            payload["origin"]["run_name"] = root.name
    # The private parser copy has its own mtimes; retain the source revision.
    payload["revision"] = "|".join(f"{name}:{signature[3]}:{signature[4]}" for name, signature in inventory.items())
    evidence = [{"name": name, "bytes": len(raw), "sha256": _sha256(raw)}
                for name, raw in captured.items()]
    payload["files"] = [{**item, "url": "data:application/octet-stream;base64," +
                        base64.b64encode(captured[item["name"]]).decode("ascii")} for item in evidence]
    payload["frames"] = []
    exported_at = datetime.now(timezone.utc).isoformat()
    payload["standalone"] = {"exported_at": exported_at, "omissions": list(OMISSIONS),
                             "privacy_notice": PRIVACY_NOTICE, "source_stability": STABILITY_NOTE,
                             "source_files": evidence}
    payload["standalone_note"] = "Offline snapshot. " + " ".join(OMISSIONS) + " " + PRIVACY_NOTICE + " " + STABILITY_NOTE
    document = _document(payload)
    publication = _publish(output, document, lambda: _verify_source(root, identity, inventory, captured))
    return {"path": str(output), "sha256": _sha256(document), "bytes": len(document),
            "exported_at": exported_at, "origin": payload["origin"], "publication": publication,
            "recording_status": payload["recording_status"], "tail_incomplete": payload["tail_incomplete"],
            "evidence_files": evidence, "omissions": list(OMISSIONS), "privacy_notice": PRIVACY_NOTICE}
