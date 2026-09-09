"""Smoke-test one built wheel in a fresh offline environment outside the checkout.

Usage: python tools/check_install.py dist
       python tools/check_install.py dist/dfeval-0.1.0-py3-none-any.whl

This verifies packaging, the recorded native spectator, and the mock harness.
It never connects to a model API or Dwarf Fortress. The temporary environment
and run output are removed on exit.
The parent Python requires pip 22.3 or newer; the fresh target has no pip or
third-party dependencies. Installation uses the parent pip's --python option.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap


class SmokeFailure(RuntimeError):
    """A failed installation or installed-package check."""


def _wheel_from(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_file() and path.suffix == ".whl":
        return path
    if not path.is_dir():
        raise SmokeFailure(f"Not a wheel or distribution directory: {path}")
    wheels = sorted(candidate for candidate in path.glob("*.whl") if candidate.is_file())
    if len(wheels) != 1:
        raise SmokeFailure(
            f"Expected exactly one wheel in {path}; found {len(wheels)}. "
            "Build the wheel first, or pass one wheel explicitly.")
    return wheels[0]


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        environment.pop(key, None)
    environment.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_CONFIG_FILE": os.devnull,
    })
    return environment


def _run(label: str, command: list[str], *, cwd: Path,
         environment: dict[str, str], timeout: int = 120) -> str:
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"{label} timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise SmokeFailure(f"{label} could not start: {exc}") from exc
    if completed.returncode:
        detail = (completed.stderr.strip() or completed.stdout.strip())[-2000:]
        raise SmokeFailure(
            f"{label} failed (exit {completed.returncode})" + (f":\n{detail}" if detail else ""))
    return completed.stdout


_CHECK_IMPORTS = textwrap.dedent("""\
    import importlib.metadata
    import importlib.resources
    import json
    from pathlib import Path
    import sys
    import dfeval

    prefix = Path(sys.prefix).resolve()
    package = Path(dfeval.__file__).resolve()
    expected_prefix = Path(sys.argv[1]).resolve()
    if prefix != expected_prefix or sys.prefix == sys.base_prefix:
        raise SystemExit('Python is not running inside the fresh smoke-test virtual environment')
    if not package.is_relative_to(prefix):
        raise SystemExit('dfeval was imported from outside the fresh virtual environment')
    distribution = importlib.metadata.distribution('dfeval')
    installed_names = {item.metadata['Name'].lower() for item in importlib.metadata.distributions()}
    if installed_names != {'dfeval'}:
        raise SystemExit('The fresh target contains unexpected distributions: ' + repr(sorted(installed_names)))
    if not Path(distribution.locate_file('')).resolve().is_relative_to(prefix):
        raise SystemExit('dfeval distribution metadata came from outside the virtual environment')
    entry_points = [entry for entry in distribution.entry_points
                    if entry.group == 'console_scripts' and entry.name == 'dfeval']
    if len(entry_points) != 1 or entry_points[0].value != 'dfeval.cli:main':
        raise SystemExit('The installed wheel is missing the dfeval console entry point')
    resources = {}
    package_files = importlib.resources.files('dfeval')
    for name in ('dfeval-live.lua', 'dfeval-bridge.lua', 'dfeval-prepare.lua'):
        content = package_files.joinpath('bridge', 'lua', name).read_bytes()
        if not content.strip():
            raise SystemExit('Bundled Lua resource is empty: ' + name)
        resources[name] = len(content)
    for name in ('index.html', 'style.css', 'app.js', 'LICENSE.txt'):
        if not package_files.joinpath('static', name).read_bytes().strip():
            raise SystemExit('Bundled spectator resource is empty: ' + name)
    example = json.loads(package_files.joinpath('examples', 'native_probe.json').read_text(encoding='utf-8'))
    if example.get('kind') != 'native_probe_excerpt':
        raise SystemExit('Bundled native recording is missing or mislabeled')
    model_example = json.loads(package_files.joinpath('examples', 'native_model.json').read_text(encoding='utf-8'))
    if model_example.get('kind') != 'native_model_excerpt':
        raise SystemExit('Bundled model recording is missing or mislabeled')
    print(json.dumps({'package': str(package), 'lua_resources': resources}))
    """)


_CHECK_SPECTATOR = textwrap.dedent("""\
    import json
    import threading
    from urllib.request import ProxyHandler, build_opener
    from dfeval.viewer import create_server
    import sys

    server = create_server(sys.argv[1], port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    try:
        for resource, media_type in (('/', 'text/html'), ('/style.css', 'text/css'),
                                     ('/app.js', 'javascript'), ('/api/run', 'application/json')):
            with opener.open(base + resource, timeout=10) as response:
                body = response.read()
                if not body or media_type not in response.headers.get('Content-Type', ''):
                    raise SystemExit('Spectator resource was not served correctly: ' + resource)
                if resource == '/api/run':
                    data = json.loads(body)
                    if len(data.get('snapshots', [])) < 2:
                        raise SystemExit('Spectator did not load the recorded native observations')
                    decisions = [event for event in data.get('events', []) if event.get('kind') == 'decision']
                    expected = int(sys.argv[2])
                    if len(decisions) != expected:
                        raise SystemExit('The recording has an unexpected number of model decisions')
        print('Installed native recording and spectator HTTP resources verified')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    """)


_CHECK_STANDALONE = textwrap.dedent("""\
    import base64
    import hashlib
    from html.parser import HTMLParser
    import json
    from pathlib import Path
    import sys

    class RecordingParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.recording = False
            self.parts = []
            self.external = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'script':
                self.recording = attrs.get('id') == 'embedded-recording'
                if 'src' in attrs:
                    self.external.append(attrs['src'])
            if tag == 'link' and attrs.get('rel') == 'stylesheet':
                self.external.append(attrs.get('href'))
        def handle_endtag(self, tag):
            if tag == 'script':
                self.recording = False
        def handle_data(self, data):
            if self.recording:
                self.parts.append(data)

    output, source = Path(sys.argv[1]), Path(sys.argv[2])
    parser = RecordingParser()
    html = output.read_text(encoding='utf-8')
    parser.feed(html)
    if 'GNU GENERAL PUBLIC LICENSE' not in html or 'Viewer license' not in html:
        raise SystemExit('Standalone viewer did not carry its software license')
    if parser.external:
        raise SystemExit('Standalone viewer still requires external assets')
    data = json.loads(''.join(parser.parts))
    if data.get('origin', {}).get('kind') != 'native_experiment' or len(data.get('snapshots', [])) < 2:
        raise SystemExit('Standalone viewer did not embed native observations')
    if len([event for event in data['events'] if event.get('kind') == 'decision']) != 3:
        raise SystemExit('Standalone viewer lost recorded decisions')
    if not data.get('experiment', {}).get('system_prompt'):
        raise SystemExit('Standalone viewer lost the recorded briefing')
    files = data.get('files', [])
    if not files or data.get('frames') or not data.get('standalone', {}).get('omissions'):
        raise SystemExit('Standalone evidence inventory or omissions are missing')
    prefix = 'data:application/octet-stream;base64,'
    for evidence in files:
        if not evidence.get('url', '').startswith(prefix):
            raise SystemExit('Standalone evidence is not embedded')
        content = base64.b64decode(evidence['url'][len(prefix):], validate=True)
        if content != (source / evidence['name']).read_bytes():
            raise SystemExit('Embedded original evidence bytes changed')
        if hashlib.sha256(content).hexdigest() != evidence.get('sha256'):
            raise SystemExit('Embedded evidence hash does not match')
    print('Installed standalone export and exact embedded evidence verified')
    """)


def check_install(wheel: Path) -> None:
    environment = _child_environment()
    with tempfile.TemporaryDirectory(prefix="dfeval-install-smoke-") as temporary:
        root = Path(temporary).resolve()
        # Neither this working directory nor its parents contain the checkout.
        # Unicode and spaces exercise normal installation/output path handling.
        working = root / "outside checkout 矮人 workspace"
        working.mkdir()
        virtual_environment = root / "fresh virtual environment"
        # -I excludes PYTHONPATH, user site packages, and the current directory.
        # -X utf8 is explicit because -I also ignores PYTHONUTF8 in the environment.
        parent_python = [sys.executable, "-I", "-X", "utf8"]
        try:
            pip_version = _run("Check parent pip", [
                *parent_python, "-m", "pip", "--version",
            ], cwd=working, environment=environment)
        except SmokeFailure as exc:
            raise SmokeFailure(
                "The parent Python requires pip >=22.3. Install or upgrade pip in your development "
                "environment before running this offline check.\n" + str(exc)) from exc
        version = re.match(r"^pip\s+(\d+)\.(\d+)", pip_version)
        if not version or tuple(map(int, version.groups())) < (22, 3):
            raise SmokeFailure(
                "The parent Python requires pip >=22.3 for --python. Upgrade pip in your development "
                "environment before running this offline check.")
        _run("Create fresh virtual environment", [
            *parent_python, "-m", "venv", "--without-pip", str(virtual_environment),
        ], cwd=working, environment=environment)
        interpreter = virtual_environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python")
        python = [str(interpreter), "-I", "-X", "utf8"]
        _run("Install wheel offline", [
            *parent_python, "-m", "pip", "--python", str(interpreter),
            "--isolated", "--disable-pip-version-check",
            "--no-input", "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel),
        ], cwd=working, environment=environment)
        imports = _run("Verify installed imports and bundled Lua", [
            *python, "-c", _CHECK_IMPORTS, str(virtual_environment),
        ], cwd=working, environment=environment)
        try:
            evidence = json.loads(imports)
        except ValueError as exc:
            raise SmokeFailure("Installed-package verification did not return valid JSON") from exc
        if set(evidence.get("lua_resources", {})) != {"dfeval-live.lua", "dfeval-bridge.lua", "dfeval-prepare.lua"}:
            raise SmokeFailure("Installed-package verification did not find all three bundled Lua scripts")
        help_output = _run("Run installed CLI help", [
            *python, "-m", "dfeval", "--help",
        ], cwd=working, environment=environment)
        if "usage: dfeval" not in help_output:
            raise SmokeFailure("Installed CLI did not emit its expected help")

        recording = working / "native recording 矮人 with spaces"
        _run("Export installed native recording", [
            *python, "-m", "dfeval", "demo", "--out", str(recording),
        ], cwd=working, environment=environment)
        audited = json.loads(_run("Audit installed native recording", [
            *python, "-m", "dfeval", "audit", str(recording), "--json",
        ], cwd=working, environment=environment))
        if (audited.get("kind") != "native_data_audit" or len(audited.get("runs", [])) != 1
                or len(audited["runs"][0].get("source_files", [])) != 2
                or audited["runs"][0].get("action_evidence", {}).get("turn_count") != 3):
            raise SmokeFailure("Installed audit did not inspect the native recording and its three turns")
        _run("Serve installed native spectator", [
            *python, "-c", _CHECK_SPECTATOR, str(recording), "3",
        ], cwd=working, environment=environment)
        standalone = working / "fortress 矮人 with spaces.html"
        _run("Export installed standalone viewer", [
            *python, "-m", "dfeval", "export", "--run", str(recording), "--out", str(standalone),
        ], cwd=working, environment=environment)
        _run("Verify standalone viewer and evidence", [
            *python, "-c", _CHECK_STANDALONE, str(standalone), str(recording),
        ], cwd=working, environment=environment)
        probe = working / "native probe"
        _run("Export installed native probe", [
            *python, "-m", "dfeval", "demo", "--recording", "probe", "--out", str(probe),
        ], cwd=working, environment=environment)
        _run("Serve installed probe spectator", [
            *python, "-c", _CHECK_SPECTATOR, str(probe), "0",
        ], cwd=working, environment=environment)

        output = working / "mock output 矮人 with spaces"
        _run("Run installed mock custodian", [
            *python, "-m", "dfeval", "run", "--agent", "custodian",
            "--months", "3", "--seed", "100", "--out", str(output),
        ], cwd=working, environment=environment)
        for name in ("ledger.jsonl", "report.txt", "result.json"):
            artifact = output / name
            if not artifact.is_file() or artifact.stat().st_size == 0:
                raise SmokeFailure(f"Installed mock run did not create a nonempty {name}")
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        if (result.get("bridge") != "mock" or result.get("months_run") != 3
                or result.get("ended_because") != "completed"):
            raise SmokeFailure("Installed custodian did not complete the expected three-month mock run")
        ledger = output / "ledger.jsonl"
        ledger_before = ledger.read_bytes()
        events = [json.loads(line) for line in ledger_before.decode("utf-8").splitlines() if line.strip()]
        if not events or events[0].get("kind") != "run_start" or events[-1].get("kind") != "run_end":
            raise SmokeFailure("Installed mock ledger is missing its run_start/run_end boundaries")
        if any(event.get("kind") == "error" for event in events):
            raise SmokeFailure("Installed mock ledger contains an error event")
        rescored = _run("Re-score installed mock ledger", [
            *python, "-m", "dfeval", "score", str(ledger),
        ], cwd=working, environment=environment)
        if "3 months on the mock bridge; ended: completed" not in rescored:
            raise SmokeFailure("Installed score command did not report the completed mock run")
        if ledger.read_bytes() != ledger_before:
            raise SmokeFailure("Re-scoring unexpectedly changed the original ledger")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", nargs="?", default="dist",
                        help="one .whl file or a directory containing exactly one wheel (default: dist)")
    arguments = parser.parse_args(argv)
    try:
        wheel = _wheel_from(Path(arguments.artifact))
        check_install(wheel)
    except (SmokeFailure, OSError, ValueError) as exc:
        print(f"Installed-wheel smoke check failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Installed-wheel smoke check interrupted.", file=sys.stderr)
        return 130
    print(f"Installed-wheel smoke check passed: {wheel.name}")
    print("Verified isolated imports, Lua and spectator assets, native data audit, demo over HTTP, standalone export, CLI help, and mock re-scoring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
