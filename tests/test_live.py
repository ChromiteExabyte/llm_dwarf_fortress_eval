"""Transport/safety tests; these do not simulate or claim real DF gameplay."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from dfeval.live import LiveBridge, LiveBridgeError, LiveBridgeTimeout, _atomic_json, resolve_dfhack_runner


def serve(bridge, *, sequence=1, change=None, result=None):
    deadline = time.monotonic() + 3
    request_path = bridge.ipc_dir / "request.json"
    while time.monotonic() < deadline:
        if request_path.exists():
            try:
                request = json.loads(request_path.read_text())
            except (PermissionError, FileNotFoundError):
                # The fixture reader needs the same transient Windows sharing
                # tolerance as the transport under test.
                time.sleep(0.001)
                continue
            if request["seq"] == sequence:
                response = {key: request[key] for key in ("protocol", "session", "seq", "op")}
                response.update(ok=True, result={"paused": True} if result is None else result)
                if change:
                    response.update(change)
                _atomic_json(bridge.ipc_dir / f"response.{sequence}.json", response)
                return request
        time.sleep(0.001)
    raise AssertionError("Client did not write expected request")


def test_transport_records_matching_session_sequence_and_operation(tmp_path):
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    with ThreadPoolExecutor() as pool:
        server = pool.submit(serve, bridge)
        assert bridge.status() == {"paused": True}
        request = server.result()
    assert request["op"] == "status"
    assert request["session"] == bridge.session
    assert request["seq"] == 1
    assert request["args"] == {}
    assert request["expires_at"] > time.time() - 1
    assert not list(bridge.ipc_dir.glob("*.tmp"))


@pytest.mark.parametrize("change", [
    {"session": "0" * 32}, {"seq": 99}, {"op": "queue_brew"}, {"protocol": 2},
    {"seq": True}, {"protocol": True},
])
def test_rejects_stale_or_mismatched_response(tmp_path, change):
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    with ThreadPoolExecutor() as pool:
        server = pool.submit(serve, bridge, change=change)
        with pytest.raises(LiveBridgeError, match="stale or mismatched"):
            bridge.observe()
        server.result()
    assert (bridge.ipc_dir / "cancel.1.json").exists()


@pytest.mark.parametrize("change, message", [
    ({"ok": "true"}, "boolean"),
    ({"result": []}, "must be an object"),
    ({"ok": False, "error": "Still is not complete"}, "Still is not complete"),
])
def test_propagates_rejection_and_checks_response_shape(tmp_path, change, message):
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    with ThreadPoolExecutor() as pool:
        server = pool.submit(serve, bridge, change=change)
        with pytest.raises(LiveBridgeError, match=message):
            bridge.status()
        server.result()


def test_timeout_writes_cancel_and_never_reuses_sequence(tmp_path):
    bridge = LiveBridge(tmp_path, timeout=0.015, poll_interval=0.001)
    with pytest.raises(LiveBridgeTimeout, match="pause is not yet confirmed"):
        bridge.advance_ticks(20)
    cancel = json.loads((bridge.ipc_dir / "cancel.1.json").read_text())
    assert cancel == {"session": bridge.session, "seq": 1}
    # A late answer cannot satisfy the next request, even with valid old fields.
    old = {"protocol": 1, "session": bridge.session, "seq": 1,
           "op": "advance_ticks", "ok": True, "result": {"elapsed_ticks": 20}}
    _atomic_json(bridge.ipc_dir / "response.1.json", old)
    bridge.timeout = 2
    with ThreadPoolExecutor() as pool:
        server = pool.submit(serve, bridge, sequence=2)
        assert bridge.pause() == {"paused": True}
        assert server.result()["op"] == "pause"


def test_keyboard_interrupt_cancels_active_sequence_before_following_pause(tmp_path, monkeypatch):
    from dfeval import live
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    real_sleep = live.time.sleep
    def interrupt_wait(seconds):
        # This point is reached only after the advance request is published.
        request = json.loads((bridge.ipc_dir / "request.json").read_text())
        assert request["op"] == "advance_ticks"
        assert request["seq"] == 1
        raise KeyboardInterrupt
    monkeypatch.setattr(live.time, "sleep", interrupt_wait)
    with pytest.raises(KeyboardInterrupt):
        bridge.advance_ticks(12000)
    cancellation = bridge.ipc_dir / "cancel.1.json"
    assert json.loads(cancellation.read_text()) == {"session": bridge.session, "seq": 1}
    monkeypatch.setattr(live.time, "sleep", real_sleep)
    with ThreadPoolExecutor() as pool:
        server = pool.submit(serve, bridge, sequence=2)
        assert bridge.pause() == {"paused": True}
        assert server.result()["seq"] == 2
    # Lua still sees the cancelled active sequence, not the new pause sequence.
    assert json.loads(cancellation.read_text())["seq"] == 1
    assert not (bridge.ipc_dir / "cancel.2.json").exists()


def test_cancel_write_failure_preserves_keyboard_interrupt(tmp_path, monkeypatch):
    from dfeval import live
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    real_atomic = live._atomic_json
    def publish(path, value):
        if path.name.startswith("cancel."):
            raise OSError("disk unavailable")
        return real_atomic(path, value)
    def interrupt_wait(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(live, "_atomic_json", publish)
    monkeypatch.setattr(live.time, "sleep", interrupt_wait)
    with pytest.raises(KeyboardInterrupt) as error:
        bridge.advance_ticks(20)
    assert "pause is not yet confirmed" in error.value.__notes__[0]


@pytest.mark.parametrize("operation,args", [
    ("lua", {"code": "df.global.pause_state=false"}),
    ("dfhack", {"command": "full-heal"}),
    ("observe", {"command": "anything"}),
    ("pause", {"paused": False}),
    ("advance_ticks", {"ticks": True}),
    ("advance_ticks", {"ticks": 0}),
    ("advance_ticks", {"ticks": 12001}),
    ("advance_ticks", {"ticks": 1.5}),
    ("queue_brew", {"workshop_id": -1, "quantity": 1}),
    ("queue_brew", {"workshop_id": 1, "quantity": 11}),
    ("queue_brew", {"workshop_id": 1, "quantity": True}),
    ("queue_brew", {"workshop_id": 1, "quantity": 1, "reaction": "MAKE_GOLD"}),
])
def test_disallowed_operations_and_invalid_bounds_never_reach_game(tmp_path, operation, args):
    bridge = LiveBridge(tmp_path)
    with pytest.raises(ValueError):
        bridge._request(operation, args)
    assert not (bridge.ipc_dir / "request.json").exists()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_timeouts_rejected(tmp_path, timeout):
    with pytest.raises(ValueError):
        LiveBridge(tmp_path, timeout=timeout)


def test_two_sessions_do_not_share_request_or_response_files(tmp_path):
    first = LiveBridge(tmp_path)
    second = LiveBridge(tmp_path)
    assert first.session != second.session
    assert first.ipc_dir != second.ipc_dir
    assert len(first.session) == 32


def test_install_and_host_command_stay_inside_selected_game(tmp_path, monkeypatch):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: "Windows")
    game = tmp_path / "Dwarves 矮人 and spaces"
    game.mkdir()
    bridge = LiveBridge(game)
    with pytest.raises(LiveBridgeError, match="DFHack scripts directory"):
        bridge.install_script()
    (game / "hack" / "scripts").mkdir(parents=True)
    bundled_script = game / "hack" / "scripts" / "dfeval-live.lua"
    bundled_script.write_text("-- an older bundled script", encoding="utf-8")
    runner = game / "hack" / "dfhack-run.exe"
    runner.touch()
    script = bridge.install_script()
    assert script == game / "dfhack-config" / "scripts" / "dfeval-live.lua"
    assert bundled_script.read_text(encoding="utf-8") == "-- an older bundled script"
    assert script.read_bytes() == (Path(__file__).parents[1] / "src" / "dfeval" /
                                   "bridge" / "lua" / "dfeval-live.lua").read_bytes()
    assert bridge.start_command() == [str(runner), "dfeval-live", "start", bridge.session]


@pytest.mark.parametrize("system,relative", [
    ("Windows", "hack/dfhack-run.exe"), ("Windows", "dfhack-run.exe"),
    ("Linux", "dfhack-run"), ("Linux", "hack/dfhack-run"),
])
def test_runner_resolves_both_release_layouts_on_each_host(tmp_path, monkeypatch, system, relative):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: system)
    runner = tmp_path / relative
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.touch()
    checked = []

    def executable(path, mode):
        checked.append(path)
        return path == runner

    monkeypatch.setattr("dfeval.live.os.access", executable)
    assert resolve_dfhack_runner(tmp_path) == runner
    assert checked == ([runner] if system == "Linux" else [])


@pytest.mark.parametrize("system,preferred,alternate", [
    ("Windows", "hack/dfhack-run.exe", "dfhack-run.exe"),
    ("Linux", "dfhack-run", "hack/dfhack-run"),
])
def test_runner_prefers_platform_release_location(tmp_path, monkeypatch, system, preferred, alternate):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: system)
    monkeypatch.setattr("dfeval.live.os.access", lambda path, mode: True)
    for relative in (preferred, alternate):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert resolve_dfhack_runner(tmp_path) == tmp_path / preferred


def test_linux_runner_requires_executable_permission_and_can_use_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: "Linux")
    (tmp_path / "dfhack-run").touch()
    monkeypatch.setattr("dfeval.live.os.access", lambda path, mode: path.parent.name == "hack")
    with pytest.raises(LiveBridgeError, match="chmod \\+x"):
        resolve_dfhack_runner(tmp_path)
    alternate = tmp_path / "hack" / "dfhack-run"
    alternate.parent.mkdir()
    alternate.touch()
    assert resolve_dfhack_runner(tmp_path) == alternate


@pytest.mark.parametrize("system,wrong_binary", [
    ("Linux", "hack/dfhack-run.exe"), ("Windows", "dfhack-run"),
])
def test_other_platform_binary_or_directory_cannot_satisfy_runner(tmp_path, monkeypatch, system, wrong_binary):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: system)
    path = tmp_path / wrong_binary
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    directory_name = "dfhack-run.exe" if system == "Windows" else "dfhack-run"
    (tmp_path / directory_name).mkdir()
    with pytest.raises(LiveBridgeError, match=f"No {system} DFHack runner"):
        resolve_dfhack_runner(tmp_path)


@pytest.mark.parametrize("system", ["Darwin", "FreeBSD", ""])
def test_unsupported_host_gives_actionable_error(tmp_path, monkeypatch, system):
    monkeypatch.setattr("dfeval.live.platform.system", lambda: system)
    (tmp_path / "dfhack-run").touch()
    with pytest.raises(LiveBridgeError, match="Use native Windows or Linux"):
        LiveBridge(tmp_path).start_command()


def test_atomic_publish_retries_windows_sharing_violation(tmp_path, monkeypatch):
    from dfeval import live
    real_replace = live.os.replace
    calls = []

    def sharing_violation_then_success(source, target):
        calls.append(target)
        if len(calls) <= 2:
            raise PermissionError("Existing file is temporarily open in game")
        return real_replace(source, target)

    monkeypatch.setattr(live.os, "replace", sharing_violation_then_success)
    destination = tmp_path / "request.json"
    destination.write_text('{"old": true}')
    _atomic_json(destination, {"new": True})
    assert json.loads(destination.read_text()) == {"new": True}
    assert len(calls) == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_response_read_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    real_open = Path.open
    attempts = []

    def briefly_locked(path, *args, **kwargs):
        if path.name == "response.1.json":
            attempts.append(path)
            if len(attempts) <= 2:
                raise PermissionError("Response rename handle is still open")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", briefly_locked)
    with ThreadPoolExecutor() as pool:
        server = pool.submit(serve, bridge)
        assert bridge.status() == {"paused": True}
        server.result()
    assert len(attempts) == 3
    assert not (bridge.ipc_dir / "cancel.1.json").exists()


def test_native_response_byte_limit_is_checked_before_json_parse(tmp_path, monkeypatch):
    from dfeval import live
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    response = {"protocol": 1, "session": bridge.session, "seq": 1,
                "op": "observe", "ok": True, "result": {"oversized": "x" * 500}}
    _atomic_json(bridge.ipc_dir / "response.1.json", response)
    monkeypatch.setattr(live, "MAX_RESPONSE_BYTES", 256)
    def must_not_parse(*args, **kwargs):
        raise AssertionError("Oversized response reached the JSON parser")
    monkeypatch.setattr(live.json, "loads", must_not_parse)
    with pytest.raises(LiveBridgeError, match="transport byte limit"):
        bridge.observe()
    assert (bridge.ipc_dir / "cancel.1.json").exists()


def test_deeply_nested_native_response_is_rejected_and_cancelled(tmp_path, monkeypatch):
    from dfeval import live
    bridge = LiveBridge(tmp_path, timeout=2, poll_interval=0.001)
    (bridge.ipc_dir / "response.1.json").write_text("[" * 2000 + "0" + "]" * 2000)
    def nesting_limit(*args, **kwargs):
        raise RecursionError("JSON parser recursion limit")
    monkeypatch.setattr(live.json, "loads", nesting_limit)
    with pytest.raises(LiveBridgeError, match="Invalid response"):
        bridge.observe()
    assert (bridge.ipc_dir / "cancel.1.json").exists()
