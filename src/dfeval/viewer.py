"""A local, read-only window onto recorded native-game evidence.

The server never contacts the game or a model. Browser window capture, when
requested by the viewer, stays in that browser. An unfinished log is not proof
that the experiment process is still running.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Any
from urllib.parse import quote, unquote, urlsplit
import webbrowser


MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_FRAME_BYTES = 20 * 1024 * 1024
MAX_EVENTS = 10000
EVIDENCE_FILES = frozenset({
    "events.jsonl", "manifest.json", "session.json", "status.json", "before.json",
    "after.json", "pause.json", "advance.json", "brew.json", "result.json",
    "summary.json", "config.json", "startup.json", "recovery-pause.json",
    "error.json", "bridge-script.lua",
})
STATIC_FILES = {"index.html": "text/html; charset=utf-8", "style.css": "text/css; charset=utf-8",
                "app.js": "text/javascript; charset=utf-8"}
FRAME_PATH = re.compile(r"frames/[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.(?:png|jpg|jpeg|webp)\Z")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'none'")


def _root_directory(run_dir: str | Path) -> Path:
    requested = Path(run_dir).expanduser().absolute()
    if requested.is_symlink() or not requested.is_dir():
        raise ValueError("Run path must be an existing directory, not a symbolic link")
    return requested.resolve()


def _safe_file(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") or any(char in part for char in ("\\", ":", "%", "\x00")) for part in parts):
        raise FileNotFoundError("Unsupported evidence path")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise FileNotFoundError("Symbolic links are not served")
    if not current.is_file() or not current.resolve().is_relative_to(root):
        raise FileNotFoundError("Evidence file not found")
    return current


def _read_file(root: Path, relative: str, limit: int = MAX_EVIDENCE_BYTES) -> bytes:
    path = _safe_file(root, relative)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise FileNotFoundError("Only regular evidence files are served")
        if info.st_size > limit:
            raise ValueError("Evidence exceeds the viewer's bounded file size")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Evidence exceeds the viewer's bounded file size")
        return data


def _parse_events(data: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    events, errors = [], []
    lines = data.splitlines(keepends=True)
    incomplete_tail = False
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        terminated = raw.endswith((b"\n", b"\r"))
        try:
            value = _strict_json(raw)
            if not isinstance(value, dict):
                raise ValueError("Event must be an object")
        except (UnicodeDecodeError, ValueError, RecursionError):
            if index == len(lines) - 1 and not terminated:
                incomplete_tail = True
            else:
                errors.append({"source": "events.jsonl", "line": index + 1, "message": "Malformed complete event record"})
            continue
        events.append(value)
        if len(events) >= MAX_EVENTS and index + 1 < len(lines):
            errors.append({"source": "events.jsonl", "message": f"Viewer limit of {MAX_EVENTS} events reached; later events are not displayed"})
            break
    return events, errors, incomplete_tail


def _strict_json(data: bytes) -> Any:
    def invalid_number(value: str) -> None:
        raise ValueError("Non-finite numbers are not JSON measurements")
    return json.loads(data.decode("utf-8-sig"), parse_constant=invalid_number)


def _frame_type(name: str, header: bytes) -> str | None:
    extension = name.rsplit(".", 1)[-1]
    if extension == "png" and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if extension in ("jpg", "jpeg") and header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if extension == "webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _native_snapshot(value: Any) -> bool:
    return isinstance(value, dict) and all(key in value for key in (
        "df_version", "dfhack_version", "world_loaded", "map_loaded", "fortress_mode", "citizens", "stocks",
    ))


def load_run(run_dir: str | Path) -> dict[str, Any]:
    """Read complete event records and preserve native values, including nulls."""
    root = _root_directory(run_dir)
    evidence, errors, events, snapshots, frames = [], [], [], [], []
    revision = []
    tail_incomplete = False
    for name in sorted(EVIDENCE_FILES):
        try:
            path = _safe_file(root, name)
            info = path.stat()
            evidence.append({"name": name, "url": "/evidence/" + quote(name), "bytes": info.st_size})
            revision.append(f"{name}:{info.st_size}:{info.st_mtime_ns}")
        except OSError:
            continue
    names = {item["name"] for item in evidence}
    if "events.jsonl" in names:
        try:
            events, parse_errors, tail_incomplete = _parse_events(_read_file(root, "events.jsonl"))
            errors.extend(parse_errors)
        except (OSError, ValueError) as exc:
            errors.append({"source": "events.jsonl", "message": str(exc)})
    for index, event in enumerate(events):
        if event.get("kind") == "snapshot" and _native_snapshot(event.get("snapshot")):
            snapshots.append({"event": event.get("event"), "at": event.get("at"), "turn": event.get("turn"),
                              "wall_seconds": event.get("wall_seconds"), "snapshot": event["snapshot"], "source": "events.jsonl",
                              "event_index": index})
        if event.get("kind") == "frame" and isinstance(event.get("path"), str) and FRAME_PATH.fullmatch(event["path"]):
            try:
                frame = _safe_file(root, event["path"])
                if frame.stat().st_size <= MAX_FRAME_BYTES:
                    # The frame endpoint independently validates the image bytes.
                    frames.append({"event": event.get("event"), "at": event.get("at"), "turn": event.get("turn"),
                                   "wall_seconds": event.get("wall_seconds"), "path": event["path"],
                                   "url": "/" + quote(event["path"], safe="/"), "event_index": index})
            except OSError:
                continue
    manifest = {}
    if "manifest.json" in names:
        try:
            candidate = _strict_json(_read_file(root, "manifest.json"))
            if isinstance(candidate, dict):
                manifest = candidate
        except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            errors.append({"source": "manifest.json", "message": str(exc)})
    starts = [event for event in events if event.get("kind") == "run_start"]
    explicitly_mock = any(event.get("bridge") == "mock" or event.get("backend") == "mock" for event in starts) or manifest.get("bridge") == "mock"
    native_manifest = manifest.get("interface") == "native measurements + wait/brew/finish" and type(manifest.get("protocol")) is int and manifest.get("events_file") == "events.jsonl"
    native_experiment = bool(starts and snapshots) or native_manifest
    native_probe = False
    if not snapshots:
        for name in ("before.json", "after.json"):
            if name not in names:
                continue
            try:
                value = _strict_json(_read_file(root, name))
                if not _native_snapshot(value):
                    raise ValueError("File does not contain the native DFHack snapshot schema; inspect its raw evidence separately")
                native_probe = True
                matching = next(((index, event) for index, event in enumerate(events)
                                 if event.get("kind") == "result" and event.get("operation") == "observe"
                                 and event.get("label") == name[:-5]), (None, {}))
                event_index, event = matching
                snapshots.append({"event": event.get("event"), "at": event.get("at"), "turn": event.get("turn"),
                                  "wall_seconds": event.get("wall_seconds"), "snapshot": value, "source": name,
                                  "event_index": event_index})
            except (OSError, ValueError, UnicodeDecodeError, RecursionError) as exc:
                errors.append({"source": name, "message": str(exc)})
    kind = "mock" if explicitly_mock else "native_experiment" if native_experiment else "live_probe" if native_probe else "unknown"
    label = {"native_experiment": "Native Dwarf Fortress experiment", "live_probe": "Native game probe",
             "mock": "Mock simulator recording", "unknown": "Unrecognized recording"}[kind]
    run_name = root.name
    if (kind == "native_experiment" and manifest.get("kind") == "native_model_excerpt"
            and manifest.get("mode") == "recorded_evidence_only"):
        label = "Recorded native model experiment · sanitized excerpt"
        title = manifest.get("title")
        if isinstance(title, str) and 0 < len(title) <= 256:
            run_name = title
    end = next((event for event in reversed(events) if event.get("kind") in ("run_end", "probe_end")), None)
    return {"schema_version": 1, "revision": "|".join(revision), "origin": {"kind": kind, "label": label, "run_name": run_name},
            "running": False if end is not None else None,
            "recording_status": "finished" if end is not None else "no_end_marker",
            "end": end, "events": events, "snapshots": snapshots, "frames": frames,
            "files": evidence, "errors": errors, "tail_incomplete": tail_incomplete}


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], run_root: Path):
        self.run_root = run_root
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer
    server_version = "DfevalSpectator"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # Requests may contain untrusted evidence names. Keep terminal output
        # quiet; the UI reports polling and evidence errors directly.
        return None

    def _trusted_request(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        try:
            parsed = urlsplit("//" + hosts[0])
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1", self.server.server_address[0]}:
                return False
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return False
            if (parsed.port or 80) != self.server.server_address[1]:
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urlsplit(origin)
                if parsed_origin.scheme != "http" or parsed_origin.netloc != hosts[0] or parsed_origin.path:
                    return False
        except ValueError:
            return False
        return True

    def _send(self, code: int, content: bytes, content_type: str = "text/plain; charset=utf-8",
              *, download: str | None = None, head: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), display-capture=(self)")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        if not head:
            self.wfile.write(content)

    def _get(self, *, head: bool = False) -> None:
        if not self._trusted_request():
            self._send(403, b"This spectator accepts requests from its local origin only.", head=head)
            return
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self._send(404, b"Not found", head=head)
            return
        try:
            route = unquote(parsed.path, errors="strict")
            name = "index.html" if route == "/" else route.lstrip("/")
            if route in ("/", "/index.html", "/style.css", "/app.js"):
                content = files("dfeval").joinpath("static", name).read_bytes()
                self._send(200, content, STATIC_FILES[name], head=head)
            elif route == "/api/run":
                content = json.dumps(load_run(self.server.run_root), ensure_ascii=True, allow_nan=False).encode("utf-8")
                self._send(200, content, "application/json; charset=utf-8", head=head)
            elif route.startswith("/evidence/"):
                relative = route[len("/evidence/"):]
                if relative not in EVIDENCE_FILES:
                    raise FileNotFoundError()
                content = _read_file(self.server.run_root, relative)
                content_type = "application/json; charset=utf-8" if relative.endswith(".json") else "application/x-ndjson; charset=utf-8" if relative.endswith(".jsonl") else "text/plain; charset=utf-8"
                self._send(200, content, content_type, download=relative, head=head)
            elif route.startswith("/frames/") and FRAME_PATH.fullmatch(route[1:]):
                content = _read_file(self.server.run_root, route[1:], MAX_FRAME_BYTES)
                content_type = _frame_type(route, content[:16])
                if content_type is None:
                    raise FileNotFoundError()
                self._send(200, content, content_type, head=head)
            else:
                self._send(404, b"Not found", head=head)
        except (OSError, UnicodeDecodeError):
            self._send(404, b"Evidence not found or not permitted", head=head)
        except ValueError:
            self._send(413, b"Evidence cannot be displayed within the viewer limits", head=head)

    def do_GET(self) -> None:
        self._get()

    def do_HEAD(self) -> None:
        self._get(head=True)

    def _write_denied(self) -> None:
        self._send(405, b"Read-only spectator: write operations are not supported.")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _write_denied


def create_server(run_dir: str | Path, host: str = "127.0.0.1", port: int = 8765) -> ViewerServer:
    """Create a read-only loopback server. Port 0 selects a free port for tests."""
    root = _root_directory(run_dir)
    address = "127.0.0.1" if host == "localhost" else host
    try:
        if not ipaddress.ip_address(address).is_loopback:
            raise ValueError()
    except ValueError as exc:
        raise ValueError("The spectator may bind only to a loopback address") from exc
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be an integer from 0 to 65535")
    return ViewerServer((address, port), ViewerHandler, root)


def serve_run(run_dir: str | Path, host: str = "127.0.0.1", port: int = 8765,
              open_browser: bool = False) -> None:
    """Serve evidence until interrupted; browser opening is explicitly opt-in."""
    server = create_server(run_dir, host, port)
    address, bound_port = server.server_address[:2]
    url = f"http://{'[' + address + ']' if ':' in address else address}:{bound_port}/"
    print(f"Read-only Dwarf Fortress spectator: {url}")
    print("Press Ctrl+C to stop. Window video is optional and requested inside the browser.")
    if open_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
