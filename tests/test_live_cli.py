"""Runner behavior with fake transport/processes; never launch a native game."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from dfeval import live_cli


def _snapshot(tick=100, drinks=35):
    return {"df_version": "53.16", "dfhack_version": "53.16-r1.1",
            "world_loaded": True, "map_loaded": True, "fortress_mode": True,
            "paused": True, "absolute_tick": tick, "save_directory": "region1",
            "citizens": [{"id": 1}, {"id": 2}],
            "stocks": {"by_item_type": {"DRINK": {"stack_units": drinks}}},
            "errors": []}


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    calls = []
    observations = [_snapshot(), _snapshot(125, 40)]
    options = {"status": _snapshot(), "advance_error": None, "pause_error": None,
               "startup_code": 0, "startup_timeout": False}

    class FakeBridge:
        def __init__(self, game_dir, *, timeout):
            self.game_dir = Path(game_dir)
            self.session = "f" * 32
            self.ipc_dir = self.game_dir / "ipc" / self.session
            calls.append(("init", {"game_dir": self.game_dir, "timeout": timeout}))

        def install_script(self):
            calls.append(("install_script", {}))
            path = self.game_dir / "dfeval-live.lua"
            path.write_text("-- fixture bridge\n", encoding="utf-8")
            return path

        def start_command(self):
            return [str(self.game_dir / "hack" / "dfhack-run.exe"), "dfeval-live", "start", self.session]

        def status(self):
            calls.append(("status", {}))
            return deepcopy(options["status"])

        def observe(self):
            calls.append(("observe", {}))
            return deepcopy(observations.pop(0))

        def pause(self):
            calls.append(("pause", {}))
            if options["pause_error"]:
                raise RuntimeError(options["pause_error"])
            return _snapshot()

        def queue_brew(self, workshop_id, quantity=1):
            calls.append(("queue_brew", {"workshop_id": workshop_id, "quantity": quantity}))
            return {"queued_jobs": quantity, "job_ids": list(range(10, 10 + quantity)), "completed": False}

        def advance_ticks(self, ticks, *, timeout):
            calls.append(("advance_ticks", {"ticks": ticks, "timeout": timeout}))
            if options["advance_error"]:
                raise TimeoutError(options["advance_error"])
            return {"elapsed_ticks": ticks, "requested_ticks": ticks, "paused": True}

    def fake_run(command, **kwargs):
        calls.append(("subprocess", {"command": command, **kwargs}))
        if options["startup_timeout"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"started", stderr=b"waiting")
        return subprocess.CompletedProcess(command, options["startup_code"], "bridge started", "fixture error" if options["startup_code"] else "")

    monkeypatch.setattr(live_cli, "LiveBridge", FakeBridge)
    monkeypatch.setattr(live_cli.subprocess, "run", fake_run)
    return game, calls, observations, options


def test_default_snapshot_never_pauses_queues_or_advances_and_preserves_raw_evidence(tmp_path, fake_host):
    game, calls, observations, _ = fake_host
    expected_before, expected_after = deepcopy(observations)
    output = tmp_path / "evidence"

    result = live_cli.run_probe(game, output)

    assert result["ok"] is True
    assert not {"pause", "queue_brew", "advance_ticks"} & {name for name, _ in calls}
    assert json.loads((output / "before.json").read_text()) == expected_before
    assert json.loads((output / "after.json").read_text()) == expected_after
    assert result["summary"]["actual_advance_ticks"] is None
    assert result["summary"]["observed_tick_delta"] == 25
    metadata = json.loads((output / "session.json").read_text())
    assert metadata["script_sha256"] == hashlib.sha256((output / "bridge-script.lua").read_bytes()).hexdigest()
    process = next(args for name, args in calls if name == "subprocess")
    assert process["shell"] is False
    assert process["cwd"] == game
    assert process["timeout"] == 30
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "probe_end"
    assert all("wall_seconds" in event for event in events)


def test_explicit_orders_pause_first_and_report_queueing_without_completion(tmp_path, fake_host):
    game, calls, _, _ = fake_host

    result = live_cli.run_probe(game, tmp_path / "evidence", advance_ticks=25, brew_workshop=0, brew_jobs=3)

    assert result["ok"] is True
    ordered = [name for name, _ in calls]
    assert ordered.index("pause") < ordered.index("queue_brew") < ordered.index("advance_ticks")
    assert result["summary"]["queued_brew_jobs"] == 3
    assert result["summary"]["actual_advance_ticks"] == 25
    assert result["summary"]["brewing_completion_verified"] is False
    assert "completion not verified" in live_cli.format_summary(result)


def test_no_loaded_fortress_rejects_requested_mutations(tmp_path, fake_host):
    game, calls, observations, options = fake_host
    options["status"].update(world_loaded=False, map_loaded=False, fortress_mode=False)
    observations[0] = deepcopy(options["status"])

    result = live_cli.run_probe(game, tmp_path / "evidence", advance_ticks=25)

    assert result["ok"] is False
    assert "loaded fortress" in result["error"]["message"]
    assert not {"pause", "queue_brew", "advance_ticks"} & {name for name, _ in calls}
    assert result["summary"]["fortress_loaded"] is False


def test_advance_failure_attempts_recovery_pause_and_keeps_original_failure(tmp_path, fake_host):
    game, calls, _, options = fake_host
    options["advance_error"] = "no response; pause unconfirmed"

    result = live_cli.run_probe(game, tmp_path / "evidence", advance_ticks=25)

    assert result["ok"] is False
    assert result["error"]["message"] == "no response; pause unconfirmed"
    assert [name for name, _ in calls].count("pause") == 2
    assert result["summary"]["pause_confirmed"] is True
    assert result["summary"]["actual_advance_ticks"] is None
    assert "after" in result["artifacts"]
    assert "recovery-pause" in result["artifacts"]


def test_unconfirmed_pause_failure_is_explicit_and_prevents_orders(tmp_path, fake_host):
    game, calls, _, options = fake_host
    options["pause_error"] = "game unresponsive"

    result = live_cli.run_probe(game, tmp_path / "evidence", brew_workshop=42)

    assert result["ok"] is False
    assert result["summary"]["pause_confirmed"] is None
    assert "Recovery pause unconfirmed" in result["secondary_errors"][0]
    assert "queue_brew" not in [name for name, _ in calls]


@pytest.mark.parametrize("timeout", [False, True])
def test_bootstrap_failure_is_saved_and_does_not_contact_bridge(tmp_path, fake_host, timeout):
    game, calls, _, options = fake_host
    options["startup_timeout"] = timeout
    options["startup_code"] = 1
    output = tmp_path / "evidence"

    result = live_cli.run_probe(game, output)

    assert result["ok"] is False
    assert "status" not in [name for name, _ in calls]
    startup = json.loads((output / "startup.json").read_text())
    assert startup["timed_out"] is timeout
    assert startup["stderr"]
    assert json.loads((output / "result.json").read_text())["ok"] is False


def test_nonempty_output_is_refused_before_any_bootstrap(tmp_path, fake_host):
    game, calls, _, _ = fake_host
    output = tmp_path / "existing"
    output.mkdir()
    protected = output / "result.json"
    protected.write_text("keep this")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        live_cli.run_probe(game, output)

    assert protected.read_text() == "keep this"
    assert calls == []


@pytest.mark.parametrize("arguments", [
    {"advance_ticks": -1}, {"advance_ticks": True}, {"advance_ticks": 12001},
    {"brew_jobs": 3}, {"brew_workshop": -1}, {"brew_workshop": 1, "brew_jobs": 0},
    {"timeout": float("nan")}, {"timeout": 0}, {"timeout": True},
])
def test_invalid_requests_fail_before_bootstrap(tmp_path, fake_host, arguments):
    game, calls, _, _ = fake_host
    with pytest.raises(ValueError):
        live_cli.run_probe(game, tmp_path / "evidence", **arguments)
    assert calls == []


def test_unknown_native_measurements_are_not_reported_as_zero(tmp_path, fake_host):
    game, _, observations, _ = fake_host
    for snapshot in observations:
        snapshot["citizens"] = None
        snapshot["stocks"] = None

    result = live_cli.run_probe(game, tmp_path / "evidence")

    assert result["summary"]["citizens_after"] is None
    assert result["summary"]["drink_stack_units_after"] is None
    assert "DRINK stack units: unknown -> unknown" in live_cli.format_summary(result)
