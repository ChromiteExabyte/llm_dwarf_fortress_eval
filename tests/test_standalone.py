"""Offline native exports with adversarial evidence; no model/game/browser runs."""

import base64
import errno
import hashlib
import html
from html.parser import HTMLParser
from importlib.resources import files
import json
import os
from pathlib import Path

import pytest

from dfeval import standalone, viewer


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.blocks = []
        self.meta = {}
        self.links = []
        self.ids = []
        self.active = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag in ("script", "style") or (tag == "pre" and attributes.get("id") == "viewer-license-text"):
            self.active = {"tag": tag, "attrs": attributes, "text": ""}
            self.blocks.append(self.active)
        if tag == "meta":
            self.meta[attributes.get("http-equiv", attributes.get("name"))] = attributes.get("content")
        if tag == "a":
            self.links.append(attributes)

    def handle_data(self, text):
        if self.active is not None:
            self.active["text"] += text

    def handle_endtag(self, tag):
        if self.active is not None and self.active["tag"] == tag:
            self.active = None

    def handle_entityref(self, name):
        self.handle_data(html.unescape("&" + name + ";"))

    def handle_charref(self, name):
        self.handle_data(html.unescape("&#" + name + ";"))

    @property
    def recording(self):
        block = next(item for item in self.blocks if item["attrs"].get("id") == "embedded-recording")
        return json.loads(block["text"])


def snapshot(name="Urist"):
    return {"df_version": "53.16", "dfhack_version": "53.16-r1.1", "world_loaded": True,
            "map_loaded": True, "fortress_mode": True, "paused": True,
            "citizens": [{"id": 7, "name": name, "stress": None, "needs": []}],
            "stocks": {"by_item_type": {"DRINK": {"stack_units": 25}}}}


def write_events(root, events, tail=b""):
    data = b"".join(json.dumps(event).encode() + b"\n" for event in events) + tail
    (root / "events.jsonl").write_bytes(data)
    return data


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "native recording"
    root.mkdir()
    write_events(root, [{"kind": "run_start", "event": 0, "config": {}},
                        {"kind": "snapshot", "event": 1, "turn": 0, "snapshot": snapshot()},
                        {"kind": "run_end", "event": 2, "outcome": "completed"}])
    return root


def evidence_bytes(recording):
    result = {}
    for item in recording["files"]:
        prefix = "data:application/octet-stream;base64,"
        assert item["url"].startswith(prefix)
        raw = base64.b64decode(item["url"][len(prefix):], validate=True)
        assert len(raw) == item["bytes"]
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        result[item["name"]] = raw
    return result


def test_export_is_self_contained_and_raw_downloads_match_original_bytes(run, tmp_path):
    before = (run / "events.jsonl").read_bytes()
    (run / "bridge-script.lua").write_bytes(b"-- original bridge\r\nreturn 1\r\n")
    (run / ".env").write_text("secret-not-in-viewer-allowlist")
    (run / "world.sav").write_bytes(b"not an export asset")
    output = tmp_path / "inspect.html"
    report = standalone.export_standalone(run, output)
    raw = output.read_bytes()
    document = Document(raw.decode())
    recording = document.recording
    assert report["path"] == str(output)
    assert report["bytes"] == len(raw)
    assert report["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["publication"] in ("atomic_exclusive_hard_link", "exclusive_creation")
    assert report["origin"]["run_name"] == run.name
    assert recording["origin"]["kind"] == "native_experiment"
    assert recording["recording_status"] == "finished" and recording["running"] is False
    assert recording["snapshots"][0]["snapshot"] == snapshot()
    assert recording["standalone"]["source_files"] == report["evidence_files"]
    assert "private prompts" in recording["standalone_note"]
    assert "will not update" in recording["standalone_note"]
    assert evidence_bytes(recording) == {
        "events.jsonl": before, "bridge-script.lua": (run / "bridge-script.lua").read_bytes()}
    assert (run / "events.jsonl").read_bytes() == before
    assert b"secret-not-in-viewer-allowlist" not in raw
    assert b'<script src="' not in raw and b'<link rel="stylesheet"' not in raw
    assert not list(tmp_path.glob(".dfeval-export-*"))


def test_embedded_evidence_cannot_close_script_or_add_executable_markup(run, tmp_path):
    hostile = '</ScRiPt><script>alert("private")</script><img src="https://example.invalid/leak">&\u2028'
    raw = write_events(run, [{"kind": "run_start"}, {"kind": "snapshot", "snapshot": snapshot(hostile)}])
    output = tmp_path / "inspect.html"
    standalone.export_standalone(run, output)
    text = output.read_text(encoding="utf-8")
    document = Document(text)
    scripts = [block for block in document.blocks if block["tag"] == "script"]
    assert len(scripts) == 2
    embedded = next(block for block in scripts if block["attrs"].get("id") == "embedded-recording")
    assert "<" not in embedded["text"] and "&" not in embedded["text"]
    assert document.recording["snapshots"][0]["snapshot"]["citizens"][0]["name"] == hostile
    assert evidence_bytes(document.recording)["events.jsonl"] == raw
    assert hostile not in text


def test_csp_authorizes_only_the_exact_bundled_script_and_styles(run, tmp_path):
    output = tmp_path / "inspect.html"
    standalone.export_standalone(run, output)
    document = Document(output.read_text(encoding="utf-8"))
    csp = document.meta["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "connect-src 'none'" in csp
    assert "media-src 'none'" in csp and "base-uri 'none'" in csp
    assert "unsafe-inline" not in csp and "'self'" not in csp
    for block in document.blocks:
        if block["tag"] not in ("script", "style") or block["attrs"].get("type") == "application/json":
            continue
        digest = base64.b64encode(hashlib.sha256(block["text"].encode()).digest()).decode()
        assert "'sha256-" + digest + "'" in csp


def test_exact_repository_license_is_packaged_and_readable_without_an_external_asset(run, tmp_path):
    packaged = files("dfeval").joinpath("static", "LICENSE.txt").read_bytes()
    original = (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes()
    assert packaged == original
    output = tmp_path / "inspect.html"
    standalone.export_standalone(run, output)
    text = output.read_text(encoding="utf-8")
    document = Document(text)
    included = next(block for block in document.blocks if block["attrs"].get("id") == "viewer-license-text")
    expected = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    assert included["text"] == expected
    assert '<summary>Viewer license</summary>' in text and "GPL-3.0-or-later" in text
    assert "standalone-viewer-license" in document.ids
    assert len(document.ids) == len(set(document.ids))
    assert "does not assign a license to the recorded evidence" in text
    assert any(link.get("href") == standalone.SOURCE_URL for link in document.links)
    assert "Project source code" in text
    assert "static/LICENSE.txt" not in text
    assert '<script src="' not in text and '<link rel="stylesheet"' not in text


@pytest.mark.parametrize("state", ["partial", "failed_before_snapshot", "probe"])
def test_partial_failed_and_probe_exports_preserve_native_status(run, tmp_path, state):
    if state == "partial":
        write_events(run, [{"kind": "run_start"}, {"kind": "snapshot", "snapshot": snapshot()}],
                     tail=b'{"kind":"decision"')
    elif state == "failed_before_snapshot":
        (run / "manifest.json").write_text(json.dumps({"protocol": 1, "events_file": "events.jsonl",
                                                       "interface": "native measurements + wait/brew/finish"}))
        write_events(run, [{"kind": "run_start"}, {"kind": "run_end", "outcome": "error"}])
    else:
        (run / "events.jsonl").unlink()
        (run / "before.json").write_text(json.dumps(snapshot()))
    expected = viewer.load_run(run)
    output = tmp_path / "inspect.html"
    standalone.export_standalone(run, output)
    actual = Document(output.read_text(encoding="utf-8")).recording
    for key in ("origin", "running", "recording_status", "tail_incomplete", "end", "errors", "snapshots", "events"):
        assert actual[key] == expected[key]
    if state == "partial":
        assert actual["tail_incomplete"] and actual["running"] is None
    elif state == "failed_before_snapshot":
        assert actual["snapshots"] == [] and actual["end"]["outcome"] == "error"


def test_frame_assets_are_omitted_but_original_frame_event_is_retained(run, tmp_path):
    (run / "frames").mkdir()
    (run / "frames" / "one.png").write_bytes(b"\x89PNG\r\n\x1a\nprivate-frame-content")
    write_events(run, [{"kind": "run_start"}, {"kind": "snapshot", "snapshot": snapshot()},
                       {"kind": "frame", "path": "frames/one.png"}])
    output = tmp_path / "inspect.html"
    report = standalone.export_standalone(run, output)
    recording = Document(output.read_text(encoding="utf-8")).recording
    assert recording["frames"] == []
    assert recording["events"][-1] == {"kind": "frame", "path": "frames/one.png"}
    assert any("frame assets" in text for text in report["omissions"])
    assert b"private-frame-content" not in output.read_bytes()


@pytest.mark.parametrize("kind", ["mock", "unknown"])
def test_non_native_export_is_rejected_without_output(run, tmp_path, kind):
    events = [{"kind": "run_start", "bridge": kind}]
    if kind == "mock":
        events.append({"kind": "snapshot", "snapshot": snapshot()})
    before = write_events(run, events)
    output = tmp_path / "inspect.html"
    with pytest.raises(ValueError, match="recognized native"):
        standalone.export_standalone(run, output)
    assert not output.exists() and (run / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("change", ["append", "same_size", "new_file", "remove"])
def test_source_change_during_parse_aborts_without_publishing_or_leaving_temporary_output(
        run, tmp_path, monkeypatch, change):
    original_load = viewer.load_run

    def changed_load(captured, **kwargs):
        result = original_load(captured, **kwargs)
        path = run / "events.jsonl"
        if change == "append":
            with path.open("ab") as stream:
                stream.write(b'{"kind":"later"}\n')
        elif change == "same_size":
            original = path.stat()
            path.write_bytes(path.read_bytes().replace(b"Urist", b"Other"))
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        elif change == "new_file":
            (run / "result.json").write_bytes(b"{}")
        else:
            path.unlink()
        return result

    monkeypatch.setattr(viewer, "load_run", changed_load)
    output = tmp_path / "inspect.html"
    with pytest.raises(ValueError, match="changed during export"):
        standalone.export_standalone(run, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".dfeval-export-*"))


@pytest.mark.parametrize("where", ["existing", "inside_source", "missing_parent", "wrong_suffix"])
def test_output_refusals_preserve_existing_files(run, tmp_path, where):
    output = tmp_path / "inspect.html"
    if where == "existing":
        output.write_bytes(b"keep this unrelated artifact")
    elif where == "inside_source":
        output = run / "inspect.html"
    elif where == "missing_parent":
        output = tmp_path / "missing" / "inspect.html"
    else:
        output = tmp_path / "inspect.txt"
    before = (run / "events.jsonl").read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        standalone.export_standalone(run, output)
    assert (run / "events.jsonl").read_bytes() == before
    if where == "existing":
        assert output.read_bytes() == b"keep this unrelated artifact"
    else:
        assert not output.exists()


def test_concurrently_created_output_is_not_replaced(run, tmp_path, monkeypatch):
    original_link = os.link
    output = tmp_path / "inspect.html"

    def raced_link(source, destination):
        Path(destination).write_bytes(b"another writer's completed artifact")
        original_link(source, destination)

    monkeypatch.setattr(standalone.os, "link", raced_link)
    with pytest.raises(FileExistsError):
        standalone.export_standalone(run, output)
    assert output.read_bytes() == b"another writer's completed artifact"
    assert not list(tmp_path.glob(".dfeval-export-*"))


def test_atomic_publication_failure_cleans_only_owned_temporary_file(run, tmp_path, monkeypatch):
    unrelated = tmp_path / ".dfeval-export-unrelated.tmp"
    unrelated.write_bytes(b"keep")

    def unsupported(*args, **kwargs):
        raise OSError("Hard links are unavailable on this filesystem")

    monkeypatch.setattr(standalone.os, "link", unsupported)
    output = tmp_path / "inspect.html"
    with pytest.raises(OSError, match="Hard links"):
        standalone.export_standalone(run, output)
    assert not output.exists() and unrelated.read_bytes() == b"keep"
    assert list(tmp_path.glob(".dfeval-export-*")) == [unrelated]


@pytest.mark.parametrize("error_number", [errno.EOPNOTSUPP, errno.EPERM])
def test_filesystems_without_hard_links_use_exclusive_complete_write(run, tmp_path, monkeypatch, error_number):
    def unsupported(*args, **kwargs):
        raise OSError(error_number, "Hard links are unavailable on this filesystem")

    monkeypatch.setattr(standalone.os, "link", unsupported)
    output = tmp_path / "inspect.html"
    report = standalone.export_standalone(run, output)
    assert report["publication"] == "exclusive_creation"
    assert report["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    recording = Document(output.read_text(encoding="utf-8")).recording
    assert evidence_bytes(recording)["events.jsonl"] == (run / "events.jsonl").read_bytes()
    assert not list(tmp_path.glob(".dfeval-export-*"))


def test_fallback_accepts_timestamps_finalized_when_write_handle_closes(run, tmp_path, monkeypatch):
    output = tmp_path / "inspect.html"
    original_fdopen = os.fdopen

    def unsupported(*args, **kwargs):
        raise OSError(errno.EPERM, "No hard links")

    def fdopen_with_close_timestamp(descriptor, mode, *args, **kwargs):
        stream = original_fdopen(descriptor, mode, *args, **kwargs)
        if mode != "wb":
            return stream

        class CloseFinalizesTimestamp:
            def __enter__(self):
                return stream.__enter__()

            def __exit__(self, *details):
                result = stream.__exit__(*details)
                info = output.stat()
                os.utime(output, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
                return result

        return CloseFinalizesTimestamp()

    monkeypatch.setattr(standalone.os, "link", unsupported)
    monkeypatch.setattr(standalone.os, "fdopen", fdopen_with_close_timestamp)
    report = standalone.export_standalone(run, output)
    assert report["publication"] == "exclusive_creation"
    assert report["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert not list(tmp_path.glob(".dfeval-export-*"))


@pytest.mark.parametrize("change", ["same_size_bytes", "same_bytes_other_owner"])
def test_fallback_readback_rejects_changed_bytes_or_owner(run, tmp_path, monkeypatch, change):
    output = tmp_path / "inspect.html"
    original_verify = standalone._verify_source
    calls = 0

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "No hard links")

    def alter_after_source_check(*args):
        nonlocal calls
        calls += 1
        original_verify(*args)
        if calls == 2:
            info = output.stat()
            data = output.read_bytes()
            if change == "same_bytes_other_owner":
                output.rename(tmp_path / "original-output.html")
                output.write_bytes(data)
            else:
                output.write_bytes(b"X" + data[1:])
            os.utime(output, ns=(info.st_atime_ns, info.st_mtime_ns))

    monkeypatch.setattr(standalone.os, "link", unsupported)
    monkeypatch.setattr(standalone, "_verify_source", alter_after_source_check)
    with pytest.raises(ValueError, match="Standalone output changed"):
        standalone.export_standalone(run, output)
    assert output.exists() is (change == "same_bytes_other_owner")
    assert not list(tmp_path.glob(".dfeval-export-*"))


def test_fallback_rechecks_source_after_writing_and_removes_its_output_on_failure(run, tmp_path, monkeypatch):
    output = tmp_path / "inspect.html"
    original_sync = os.fsync

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "No hard links")

    def source_changes_during_output_write(descriptor):
        original_sync(descriptor)
        if output.exists() and os.fstat(descriptor).st_ino == output.stat().st_ino:
            with (run / "events.jsonl").open("ab") as stream:
                stream.write(b'{"kind":"later"}\n')

    monkeypatch.setattr(standalone.os, "link", unsupported)
    monkeypatch.setattr(standalone.os, "fsync", source_changes_during_output_write)
    with pytest.raises(ValueError, match="changed during export"):
        standalone.export_standalone(run, output)
    assert not output.exists() and not list(tmp_path.glob(".dfeval-export-*"))


def test_fallback_cannot_overwrite_a_competing_destination(run, tmp_path, monkeypatch):
    output = tmp_path / "inspect.html"

    def other_writer_arrives(source, destination):
        Path(destination).write_bytes(b"keep the other writer's artifact")
        raise OSError(errno.EOPNOTSUPP, "No hard links")

    monkeypatch.setattr(standalone.os, "link", other_writer_arrives)
    with pytest.raises(FileExistsError):
        standalone.export_standalone(run, output)
    assert output.read_bytes() == b"keep the other writer's artifact"
    assert not list(tmp_path.glob(".dfeval-export-*"))


def test_fallback_cleanup_does_not_delete_a_replaced_output(run, tmp_path, monkeypatch):
    output = tmp_path / "inspect.html"
    original_verify = standalone._verify_source
    calls = 0

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "No hard links")

    def replacement_before_final_check(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Keep the first inode allocated so the filesystem cannot reuse it.
            output.rename(tmp_path / "moved-export.html")
            output.write_bytes(b"keep the replacement")
            raise ValueError("Source evidence changed during export")
        original_verify(*args)

    monkeypatch.setattr(standalone.os, "link", unsupported)
    monkeypatch.setattr(standalone, "_verify_source", replacement_before_final_check)
    with pytest.raises(ValueError, match="changed during export"):
        standalone.export_standalone(run, output)
    assert output.read_bytes() == b"keep the replacement"
    assert not list(tmp_path.glob(".dfeval-export-*"))


@pytest.mark.parametrize("limit", ["MAX_SOURCE_BYTES", "MAX_DOCUMENT_BYTES"])
def test_bounded_export_fails_without_output(run, tmp_path, monkeypatch, limit):
    monkeypatch.setattr(standalone, limit, 16)
    output = tmp_path / "inspect.html"
    with pytest.raises(ValueError, match="limit"):
        standalone.export_standalone(run, output)
    assert not output.exists() and not list(tmp_path.glob(".dfeval-export-*"))


def test_allowlisted_symlink_cannot_include_an_unrelated_private_file(run, tmp_path):
    secret = tmp_path / "private.json"
    secret.write_bytes(b"private outside recording")
    try:
        (run / "result.json").symlink_to(secret)
    except OSError:
        pytest.skip("This platform/account cannot create symbolic links")
    output = tmp_path / "inspect.html"
    with pytest.raises(ValueError, match="safe regular file"):
        standalone.export_standalone(run, output)
    assert not output.exists() and secret.read_bytes() == b"private outside recording"
