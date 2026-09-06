"""Portable game-path selection without contacting an installed game."""

from dfeval import cli
import pytest


def test_game_path_environment_and_explicit_precedence(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_live", lambda args: seen.append(args.df_path) or 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: seen.append(args.df_path) or 0)
    monkeypatch.setenv("DFEVAL_DF_PATH", "external games/fortress-é")
    assert cli.main(["doctor"]) == 0
    assert cli.main(["live"]) == 0
    assert cli.main(["live", "--df-path", "chosen game"]) == 0
    assert seen == ["external games/fortress-é", "external games/fortress-é", "chosen game"]


def test_game_path_default_when_environment_missing_or_empty(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: seen.append(args.df_path) or 0)
    monkeypatch.delenv("DFEVAL_DF_PATH", raising=False)
    assert cli.main(["doctor"]) == 0
    monkeypatch.setenv("DFEVAL_DF_PATH", "")
    assert cli.main(["doctor"]) == 0
    assert seen == ["dwarfFortressItself", "dwarfFortressItself"]


@pytest.mark.parametrize("mode", ["local", "cloud", "idle", "rule"])
def test_experiment_selects_explicit_policy_and_passes_budgets(monkeypatch, tmp_path, mode):
    from dfeval import experiment
    seen = []
    monkeypatch.setattr(experiment, "run_experiment", lambda game, policy, **kwargs:
                        seen.append((game, policy.public_config(), kwargs)) or {"ok": True})
    monkeypatch.setenv("DFEVAL_TEST_KEY", "test-credential")
    arguments = ["experiment", "--policy", mode, "--df-path", "external game",
                 "--decisions", "3", "--max-ticks", "3600", "--out", str(tmp_path / "run")]
    if mode in ("local", "cloud"):
        arguments.extend(["--model", "explicit-test-model"])
    if mode == "cloud":
        arguments.extend(["--api-key-env", "DFEVAL_TEST_KEY"])
    assert cli.main(arguments) == 0
    game, policy, options = seen[0]
    assert game == "external game"
    assert options["config"].max_decisions == 3
    assert options["config"].max_total_ticks == 3600
    assert policy["is_model"] is (mode in ("local", "cloud"))
    if policy["is_model"]:
        assert policy["mode"] == mode
        assert policy["model"] == "explicit-test-model"
        assert "test-credential" not in str(policy)


def test_cloud_preflight_rejects_missing_key_without_contacting_game(monkeypatch, capsys):
    from dfeval import experiment
    monkeypatch.setattr(experiment, "run_experiment", lambda *a, **k: pytest.fail("Game must not be contacted"))
    monkeypatch.delenv("DFEVAL_MISSING_TEST_KEY", raising=False)
    assert cli.main(["experiment", "--policy", "cloud", "--model", "test-model",
                     "--api-key-env", "DFEVAL_MISSING_TEST_KEY"]) == 1
    assert "Set the DFEVAL_MISSING_TEST_KEY" in capsys.readouterr().err


def test_model_identity_is_required_before_game_connection(monkeypatch):
    from dfeval import experiment
    monkeypatch.setattr(experiment, "run_experiment", lambda *a, **k: pytest.fail("Game must not be contacted"))
    assert cli.main(["experiment", "--policy", "local"]) == 1


def test_scenario_capture_passes_stopped_attestation_without_implicit_approval(monkeypatch):
    from dfeval import scenario
    seen = []
    monkeypatch.setattr(scenario, "snapshot_save", lambda *a, **k: seen.append((a, k)) or {})
    assert cli.main(["scenario", "capture", "--save-name", "region-test", "--out", "runs/snapshot"]) == 0
    assert seen[0][1]["game_stopped"] is False
    assert cli.main(["scenario", "capture", "--save-name", "region-test", "--out", "runs/snapshot", "--game-stopped"]) == 0
    assert seen[1][1]["game_stopped"] is True


def test_watched_run_uses_same_expanded_path_and_closes_server_after_error(monkeypatch, tmp_path):
    import json
    from urllib.request import ProxyHandler, build_opener
    import webbrowser
    from dfeval import experiment, viewer

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    opened = []
    servers = []
    original_create = viewer.create_server

    def create(directory, **kwargs):
        assert directory == tmp_path / "recording"
        server = original_create(directory, **kwargs)
        servers.append(server)
        return server

    def run(game, policy, **kwargs):
        assert kwargs["output_dir"] == tmp_path / "recording"
        assert opened
        # The actual viewer must be serving the same directory before the run.
        (kwargs["output_dir"] / "manifest.json").write_text('{"title":"same output"}')
        opener = build_opener(ProxyHandler({}))
        with opener.open(opened[0] + "evidence/manifest.json", timeout=5) as response:
            assert json.loads(response.read())["title"] == "same output"
        raise RuntimeError("deliberate fake-run failure")

    monkeypatch.setattr(viewer, "create_server", create)
    monkeypatch.setattr(webbrowser, "open", lambda url, **kwargs: opened.append(url))
    monkeypatch.setattr(experiment, "run_experiment", run)
    assert cli.main(["experiment", "--policy", "idle", "--watch", "--port", "0",
                     "--out", "~/recording"]) == 1
    assert servers[0].socket.fileno() == -1


def test_watch_closes_socket_without_blocking_if_server_thread_cannot_start(monkeypatch, tmp_path):
    from dfeval import experiment, viewer

    class FailedThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("Thread could not start")
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pytest.fail("A thread that never started cannot be joined")

    class Server:
        closed = False
        def serve_forever(self):
            pytest.fail("Server was not started")
        def shutdown(self):
            pytest.fail("shutdown would wait forever without a running server thread")
        def server_close(self):
            self.closed = True

    server = Server()
    monkeypatch.setattr(viewer, "create_server", lambda *args, **kwargs: server)
    monkeypatch.setattr(cli.threading, "Thread", FailedThread)
    monkeypatch.setattr(experiment, "run_experiment", lambda *a, **k: pytest.fail("Game must not be contacted"))
    assert cli.main(["experiment", "--policy", "idle", "--watch", "--out", str(tmp_path / "run")]) == 1
    assert server.closed
