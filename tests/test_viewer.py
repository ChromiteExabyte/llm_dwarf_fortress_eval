"""Local spectator replay/security tests; no game, model, or browser is run."""

import base64
import http.client
from importlib.resources import files
import json
from pathlib import Path
import threading

import pytest

from dfeval.viewer import create_server, load_run


def _snapshot(stress=100):
    return {"df_version": "53.16", "dfhack_version": "53.16-r1.1", "world_loaded": True,
            "map_loaded": True, "fortress_mode": True, "paused": True, "year": 100,
            "year_tick": 50, "absolute_tick": 40320050,
            "citizens": [{"id": 7, "name": "Urist", "stress": stress, "needs": None}],
            "stocks": {"by_item_type": {"DRINK": {"stack_units": 25}}}, "errors": []}


def _events(root, events, tail=b""):
    path = root / "events.jsonl"
    path.write_bytes(b"".join(json.dumps(event).encode() + b"\n" for event in events) + tail)
    return path


@pytest.fixture
def recorded_run(tmp_path):
    root = tmp_path / "recording"
    root.mkdir()
    _events(root, [
        {"event": 0, "at": "2026-09-05T00:00:00Z", "wall_seconds": 0, "kind": "run_start", "config": {}},
        {"event": 1, "at": "2026-09-05T00:00:01Z", "wall_seconds": 1, "kind": "snapshot", "turn": 0, "snapshot": _snapshot()},
        {"event": 2, "kind": "decision", "turn": 0, "decision": {"action": "observe", "reason": "Inspect native records."}},
        {"event": 3, "at": "2026-09-05T00:00:02Z", "wall_seconds": 2, "kind": "snapshot", "turn": 1, "snapshot": _snapshot(90)},
        {"event": 4, "kind": "run_end", "outcome": "completed", "summary": {}},
    ])
    return root


@pytest.fixture
def local_server(recorded_run):
    server = create_server(recorded_run, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, path="/", method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def test_finished_run_replays_native_snapshots_without_game_or_model(recorded_run):
    state = load_run(recorded_run)
    assert state["origin"]["kind"] == "native_experiment"
    assert state["recording_status"] == "finished"
    assert state["running"] is False
    assert state["snapshots"][0]["event"] == 1
    assert state["snapshots"][0]["snapshot"] == _snapshot()
    assert state["snapshots"][1]["snapshot"]["citizens"][0]["stress"] == 90
    assert state["snapshots"][0]["snapshot"]["citizens"][0]["needs"] is None
    assert state["errors"] == []


def test_incomplete_tail_keeps_complete_evidence_and_does_not_claim_process_is_alive(recorded_run):
    _events(recorded_run, [{"event": 0, "kind": "run_start"}, {"event": 1, "kind": "snapshot", "snapshot": _snapshot()}],
            tail=b'{"event":2,"kind":"decis')
    first = load_run(recorded_run)
    assert len(first["events"]) == 2
    assert len(first["snapshots"]) == 1
    assert first["tail_incomplete"] is True
    assert first["running"] is None
    assert first["recording_status"] == "no_end_marker"
    with (recorded_run / "events.jsonl").open("ab") as stream:
        stream.write(b'ion","decision":{"action":"observe"}}\n')
    second = load_run(recorded_run)
    assert second["tail_incomplete"] is False
    assert len(second["events"]) == 3


def test_complete_invalid_record_is_reported_without_discarding_valid_records(recorded_run):
    path = _events(recorded_run, [{"event": 0, "kind": "run_start"}])
    with path.open("ab") as stream:
        stream.write(b'{"event":1,"bad":NaN}\nnot-json\n')
    result = load_run(recorded_run)
    assert len(result["events"]) == 1
    assert len(result["errors"]) == 2
    assert result["tail_incomplete"] is False


def test_probe_before_after_files_have_explicit_origin_and_no_fabricated_time(tmp_path):
    (tmp_path / "before.json").write_text(json.dumps(_snapshot()))
    (tmp_path / "after.json").write_text(json.dumps(_snapshot(80)))
    result = load_run(tmp_path)
    assert result["origin"]["kind"] == "live_probe"
    assert [item["source"] for item in result["snapshots"]] == ["before.json", "after.json"]
    assert all(item["at"] is None and item["event"] is None for item in result["snapshots"])


def test_mock_or_generic_run_start_is_never_mislabeled_as_native(tmp_path):
    _events(tmp_path, [{"seq": 1, "kind": "run_start", "bridge": "mock", "fortress": "Example"},
                       {"seq": 2, "kind": "snapshot", "snapshot": {"dwarves": [], "stocks": {}}}])
    result = load_run(tmp_path)
    assert result["origin"]["kind"] == "mock"
    assert result["snapshots"] == []
    _events(tmp_path, [{"event": 0, "kind": "run_start", "config": {}}])
    assert load_run(tmp_path)["origin"]["kind"] == "unknown"


def test_native_manifest_identifies_pre_snapshot_experiment_without_claiming_game_is_loaded(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"protocol": 1, "events_file": "events.jsonl",
                                                        "interface": "native measurements + wait/brew/finish"}))
    _events(tmp_path, [{"event": 0, "kind": "run_start", "config": {}}])
    result = load_run(tmp_path)
    assert result["origin"]["kind"] == "native_experiment"
    assert result["snapshots"] == [] and result["running"] is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_public_network_binding_is_rejected(recorded_run, host):
    with pytest.raises(ValueError, match="loopback"):
        create_server(recorded_run, host=host, port=0)


def test_ui_and_api_are_read_only_local_resources_with_restrictive_headers(local_server):
    status, headers, body = _request(local_server)
    assert status == 200
    assert b"Fortress Observatory" in body
    assert b"/style.css" in body and b"/app.js" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "Access-Control-Allow-Origin" not in headers
    for path, expected in [("/style.css", "text/css"), ("/app.js", "text/javascript"), ("/api/run", "application/json")]:
        code, values, data = _request(local_server, path)
        assert code == 200
        assert values["Content-Type"].startswith(expected)
        assert data
    assert json.loads(_request(local_server, "/api/run")[2])["origin"]["kind"] == "native_experiment"


@pytest.mark.parametrize("path", [
    "/evidence/../secret.txt", "/evidence/%2e%2e/secret.txt", "/evidence/%252e%252e/secret.txt",
    "/evidence/.env", "/evidence/secret.json", "/evidence/C:/secret.txt", "/evidence/events.jsonl%00",
    "/frames/../secret.png", "/frames/%2e%2e%2fsecret.png", "/frames/fake.svg", "/.git/config",
    "/evidence/%ff",
])
def test_traversal_private_paths_and_unknown_assets_are_never_served(local_server, recorded_run, path):
    (recorded_run / ".env").write_text("not public")
    (recorded_run.parent / "secret.txt").write_text("not public")
    code, _, body = _request(local_server, path)
    assert code == 404
    assert b"not public" not in body


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_write_methods_are_rejected_without_changing_evidence(local_server, recorded_run, method):
    path = recorded_run / "events.jsonl"
    before = path.read_bytes()
    assert _request(local_server, "/evidence/events.jsonl", method=method)[0] == 405
    assert path.read_bytes() == before


@pytest.mark.parametrize("headers", [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}])
def test_foreign_origin_and_dns_rebinding_requests_are_rejected(local_server, headers):
    assert _request(local_server, "/api/run", headers=headers)[0] == 403


def test_evidence_download_is_attachment_and_preserves_the_actual_ledger(local_server, recorded_run):
    status, headers, body = _request(local_server, "/evidence/events.jsonl")
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="events.jsonl"'
    assert body == (recorded_run / "events.jsonl").read_bytes()


def test_untrusted_names_and_decisions_stay_json_text_not_executable_markup(local_server, recorded_run):
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    native = _snapshot()
    native["citizens"][0]["name"] = payload
    _events(recorded_run, [{"event": 0, "kind": "run_start"}, {"event": 1, "kind": "snapshot", "snapshot": native},
                          {"event": 2, "kind": "decision", "decision": {"action": "observe", "reason": payload}}])
    status, headers, body = _request(local_server, "/api/run")
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["snapshots"][0]["snapshot"]["citizens"][0]["name"] == payload
    assert payload.encode() not in _request(local_server)[2]
    script = files("dfeval").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert ".textContent" in script
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "eval(" not in script


def test_game_frames_must_be_allowed_real_image_files(local_server, recorded_run):
    frames = recorded_run / "frames"
    frames.mkdir()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    (frames / "0001.png").write_bytes(png)
    (frames / "fake.png").write_text("<script>not an image</script>")
    _events(recorded_run, [{"event": 0, "kind": "run_start"}, {"event": 1, "kind": "frame", "path": "frames/0001.png"},
                          {"event": 2, "kind": "frame", "path": "../secret.png"}])
    status, headers, body = _request(local_server, "/frames/0001.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body == png
    assert _request(local_server, "/frames/fake.png")[0] == 404
    state = json.loads(_request(local_server, "/api/run")[2])
    assert len(state["frames"]) == 1
    assert state["frames"][0]["url"] == "/frames/0001.png"


def test_symlinked_evidence_and_frames_are_rejected(local_server, recorded_run):
    outside = recorded_run.parent / "outside.json"
    outside.write_text('{"private": true}')
    target = recorded_run / "before.json"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("This host does not permit creating symbolic links")
    assert _request(local_server, "/evidence/before.json")[0] == 404
    assert "before.json" not in {item["name"] for item in load_run(recorded_run)["files"]}


def test_static_ui_has_no_remote_assets_or_automatic_capture():
    html = files("dfeval").joinpath("static", "index.html").read_text(encoding="utf-8")
    script = files("dfeval").joinpath("static", "app.js").read_text(encoding="utf-8")
    assert "https://" not in html and "http://" not in html
    assert 'fetch("/api/run"' in script
    assert 'getDisplayMedia({video:{displaySurface:"window"},audio:false' in script
    assert 'monitorTypeSurfaces:"exclude"' in script
    assert 'displaySurface==="monitor"' in script
    assert 'addEventListener("click",chooseWindow)' in script
    assert "Record video" in html and "Download video" in html
    assert "No running game, model connection, or cloud service is required" in html
