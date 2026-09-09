"""Local spectator replay/security tests; no game, model, or browser is run."""

import base64
import copy
import http.client
from importlib.resources import files
import json
from pathlib import Path
import threading

import pytest

from dfeval.viewer import create_server, load_run
from dfeval.policies import LEGACY_SYSTEM_PROMPT, SYSTEM_PROMPT


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


def _record_briefing(root, *, manifest=None, start=None):
    if manifest is not None:
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _events(root, [{"event": 0, "kind": "run_start", **(start or {})},
                   {"event": 1, "kind": "snapshot", "snapshot": _snapshot()}])


@pytest.mark.parametrize("prompt", [LEGACY_SYSTEM_PROMPT, SYSTEM_PROMPT, "Saved public briefing.\r\n\tExact spacing: café.\n"])
def test_experiment_metadata_retains_the_exact_recorded_prompt_and_limits(tmp_path, prompt):
    policy = {"kind": "ollama", "is_model": True, "model": "recorded-model", "system_prompt": prompt,
              "options": {"seed": 17, "num_ctx": 32768}}
    config = {"policy": policy, "scenario": "recorded-scenario", "max_decisions": 3,
              "max_total_ticks": 3600, "request_timeout": 12.5, "history_decisions": 0,
              "simulation_fps": None}
    _record_briefing(tmp_path, manifest={"policy": policy, "config": config}, start={"config": config})
    state = load_run(tmp_path)
    metadata = state["experiment"]
    assert metadata["status"] == "recorded" and metadata["conflicts"] == []
    assert metadata["policy_kind"] == "ollama" and metadata["is_model"] is True
    assert metadata["model"] == "recorded-model"
    assert metadata["system_prompt"] == prompt
    assert metadata["scenario"] == "recorded-scenario"
    assert metadata["budgets"] == {"max_decisions": 3, "max_total_ticks": 3600,
                                  "request_timeout": 12.5, "history_decisions": 0}
    assert "manifest.json.policy" in metadata["sources"]
    assert "events.jsonl:run_start.config.policy" in metadata["sources"]
    assert state["errors"] == []


@pytest.mark.parametrize("policy_location", ["config", "run_start"])
def test_manifestless_native_evidence_uses_its_recorded_start_policy(tmp_path, policy_location):
    policy = {"kind": "recorded-kind", "system_prompt": "An older recorded objective."}
    start = {"config": {"max_policy_calls": 2}, "scenario": "older-scenario"}
    if policy_location == "config":
        start["config"]["policy"] = policy
    else:
        start["policy"] = policy
    _record_briefing(tmp_path, start=start)
    metadata = load_run(tmp_path)["experiment"]
    assert metadata["system_prompt"] == policy["system_prompt"]
    assert metadata["policy_kind"] == "recorded-kind"
    assert metadata["model"] is None and metadata["is_model"] is None
    assert metadata["scenario"] == "older-scenario"
    assert metadata["budgets"] == {"max_policy_calls": 2}
    assert all(source.startswith("events.jsonl:run_start") for source in metadata["sources"])


def test_absent_briefing_is_unknown_without_current_prompt_or_budget_defaults(recorded_run):
    metadata = load_run(recorded_run)["experiment"]
    assert metadata == {"policy_kind": None, "is_model": None, "model": None, "system_prompt": None,
                        "budgets": {}, "scenario": None, "status": "unknown", "sources": [], "conflicts": []}


@pytest.mark.parametrize("difference", ["prompt", "model", "is_model", "nested_setting", "budget", "scenario"])
def test_conflicting_recorded_briefings_are_unknown_and_explicitly_reported(tmp_path, difference):
    original = {"policy": {"kind": "ollama", "is_model": True, "model": "model-a",
                           "system_prompt": "Recorded objective A.", "options": {"seed": 17}},
                "max_decisions": 3, "scenario": "scenario-a"}
    changed = copy.deepcopy(original)
    if difference == "prompt":
        changed["policy"]["system_prompt"] = "A contradictory objective."
    elif difference == "model":
        changed["policy"]["model"] = "model-b"
    elif difference == "is_model":
        changed["policy"]["is_model"] = False
    elif difference == "nested_setting":
        changed["policy"]["options"]["seed"] = 99
    elif difference == "budget":
        changed["max_decisions"] = 4
    else:
        changed["scenario"] = "scenario-b"
    _record_briefing(tmp_path, manifest={"policy": original["policy"], "config": original},
                     start={"config": changed})
    result = load_run(tmp_path)
    metadata = result["experiment"]
    assert metadata["status"] == "conflicting"
    assert metadata["conflicts"] == [difference if difference == "scenario" else "budgets" if difference == "budget" else "policy"]
    assert metadata["system_prompt"] is None and metadata["model"] is None
    assert metadata["is_model"] is None and metadata["policy_kind"] is None
    assert metadata["scenario"] is None and metadata["budgets"] == {}
    assert any("Conflicting recorded experiment metadata" in error["message"] for error in result["errors"])


def test_manifest_policy_conflict_and_duplicate_starts_cannot_select_a_briefing(tmp_path):
    _record_briefing(tmp_path, manifest={"policy": {"system_prompt": "One"},
                                       "config": {"policy": {"system_prompt": "Two"}}})
    assert load_run(tmp_path)["experiment"]["status"] == "conflicting"
    (tmp_path / "manifest.json").unlink()
    _events(tmp_path, [{"kind": "run_start", "policy": {"system_prompt": "One"}},
                       {"kind": "run_start", "policy": {"system_prompt": "One"}},
                       {"kind": "snapshot", "snapshot": _snapshot()}])
    result = load_run(tmp_path)
    assert result["experiment"]["status"] == "conflicting"
    assert result["experiment"]["system_prompt"] is None
    assert any("Multiple run_start" in error["message"] for error in result["errors"])


def test_metadata_does_not_add_connection_credentials_or_host_paths(tmp_path):
    policy = {"kind": "ollama", "is_model": True, "model": "local-model", "system_prompt": "Recorded briefing.",
              "endpoint": "http://private-host:18080/api/chat", "base_url": "http://private-host:18080",
              "api_key": "secret-credential", "api_key_env": "PRIVATE_KEY_ENV",
              "headers": {"Authorization": "Bearer secret-credential"}, "options": {"num_ctx": 8192}}
    config = {"policy": policy, "max_decisions": 2, "stop_file": "C:\\private\\STOP",
              "starting_snapshot_path": "/private/fortress", "host": {"username": "private-user"}}
    _record_briefing(tmp_path, manifest={"policy": policy, "config": config, "endpoint": policy["endpoint"]})
    result = load_run(tmp_path)
    metadata = result["experiment"]
    serialized = json.dumps(metadata)
    assert metadata["status"] == "recorded" and metadata["system_prompt"] == "Recorded briefing."
    assert metadata["budgets"] == {"max_decisions": 2}
    for private in ("private-host", "secret-credential", "PRIVATE_KEY_ENV", "Authorization", "stop_file",
                    "private-user", "fortress", "headers", "endpoint", "base_url", "options"):
        assert private not in serialized


@pytest.mark.parametrize("value", [True, -1, 1.5, "12", 10**400])
def test_invalid_numeric_budget_is_unknown_not_a_fabricated_limit(tmp_path, value):
    _record_briefing(tmp_path, manifest={"policy": {"system_prompt": "Saved"}, "config": {"max_decisions": value}})
    result = load_run(tmp_path)
    assert result["experiment"]["status"] == "invalid"
    assert result["experiment"]["budgets"] == {}
    assert result["experiment"]["system_prompt"] is None
    assert result["errors"]


@pytest.mark.parametrize("oversized", ["prompt", "object"])
def test_oversized_metadata_is_unknown_instead_of_silently_truncated(tmp_path, oversized):
    from dfeval.viewer import MAX_EXPERIMENT_METADATA_BYTES, MAX_RECORDED_PROMPT_BYTES
    policy = {"kind": "ollama", "system_prompt": "Saved"}
    policy["system_prompt" if oversized == "prompt" else "extra"] = "x" * (
        MAX_RECORDED_PROMPT_BYTES + 1 if oversized == "prompt" else MAX_EXPERIMENT_METADATA_BYTES + 1)
    _record_briefing(tmp_path, manifest={"policy": policy})
    result = load_run(tmp_path)
    assert result["experiment"]["status"] == "invalid"
    assert result["experiment"]["system_prompt"] is None
    assert result["errors"]


@pytest.mark.parametrize("recording", ["model", "probe"])
def test_packaged_native_recordings_keep_their_actual_or_unknown_briefing(tmp_path, recording):
    from dfeval.demo import write_demo
    exported = write_demo(tmp_path / recording, recording=recording)
    result = load_run(exported)
    metadata = result["experiment"]
    assert result["errors"] == []
    if recording == "model":
        original = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        assert metadata["system_prompt"] == original["policy"]["system_prompt"]
        assert metadata["model"] == original["policy"]["model"]
        assert metadata["status"] == "recorded"
    else:
        assert metadata["status"] == "unknown"
        assert metadata["system_prompt"] is None and metadata["is_model"] is None


def test_non_native_evidence_has_no_experiment_briefing(tmp_path):
    _events(tmp_path, [{"kind": "run_start", "bridge": "mock", "policy": {"system_prompt": "Mock objective"}}])
    assert load_run(tmp_path)["experiment"] is None
    _events(tmp_path, [{"kind": "run_start", "policy": {"system_prompt": "Unrecognized objective"}}])
    assert load_run(tmp_path)["experiment"] is None


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


def test_explicit_frame_omission_preserves_events_without_frame_file_io(recorded_run, monkeypatch):
    import dfeval.viewer as viewer
    frame = {"event": 2, "kind": "frame", "path": "frames/unavailable.png", "turn": 0}
    _events(recorded_run, [{"event": 0, "kind": "run_start"},
                          {"event": 1, "kind": "snapshot", "snapshot": _snapshot()}, frame])
    original = viewer._safe_file
    def no_frame_io(root, relative):
        assert not relative.startswith("frames/"), "Frame assets must not be inspected in this mode"
        return original(root, relative)
    monkeypatch.setattr(viewer, "_safe_file", no_frame_io)
    result = load_run(recorded_run, include_frames=False)
    assert result["frames"] == []
    assert result["events"][-1] == frame
    assert len(result["snapshots"]) == 1
    assert result["errors"] == []


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
    from html.parser import HTMLParser

    class Assets(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name in {"src", "href", "srcset", "action"} and value:
                    # A user-clicked source/license link does not load an asset.
                    if tag == "a" and name == "href" and value == (
                            "https://github.com/ChromiteExabyte/llm_dwarf_fortress_eval/tree/main/src/dfeval/static"):
                        continue
                    assert not value.startswith(("http:", "https:", "//")), (tag, name, value)

    html = files("dfeval").joinpath("static", "index.html").read_text(encoding="utf-8")
    script = files("dfeval").joinpath("static", "app.js").read_text(encoding="utf-8")
    Assets().feed(html)
    assert 'fetch("/api/run"' in script
    assert 'getDisplayMedia({video:{displaySurface:"window"},audio:false' in script
    assert 'monitorTypeSurfaces:"exclude"' in script
    assert 'displaySurface==="monitor"' in script
    assert 'addEventListener("click",chooseWindow)' in script
    assert "Record video" in html and "Download video" in html
    assert "No running game, model connection, or cloud service is required" in html
