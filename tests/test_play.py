"""Learning, memory choice and cost stops, with no game or paid provider."""

import json
from decimal import Decimal

import pytest

from dfeval.play import LearningLoop, LimitReached, OBJECTIVE, PlayLimits, SpendAccount
from dfeval.inference import TextInferenceError


class Client:
    mode = "local"
    timeout = 10
    max_output_tokens = 4096

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def public_config(self):
        return {"model": "fixture", "mode": self.mode}

    def check_public_input(self, value):
        pass

    def infer(self, request, **kwargs):
        self.requests.append(request.messages)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return {"text": json.dumps(reply), "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "truncated": False, "finish_reason": "stop"}


class Game:
    def __init__(self):
        self.ticks = 0
        self.paused = False
        self.actions = []

    def observe(self):
        return {"citizens": [{"name": "Urist", "stress": 7}], "absolute_tick": self.ticks}

    def advance_ticks(self, ticks):
        self.ticks += ticks
        return {"elapsed_ticks": ticks, "paused": True}

    def pause(self):
        self.paused = True
        return {"paused": True}

    def dispatch(self, tool, args):
        raise ValueError("Unknown game tool")


class Workspace:
    def __init__(self):
        self.files = {}

    def dispatch(self, tool, args):
        if tool == "workspace_write":
            self.files[args["path"]] = args["content"]
            return {"written": args["path"]}
        if tool == "workspace_read":
            return self.files[args["path"]]
        if tool == "workspace_run":
            return json.loads(self.files[args["path"]])
        raise ValueError("Unknown workspace tool")


def call(tool, **arguments):
    return {"tool": tool, "arguments": arguments}


def loop(tmp_path, replies, **kwargs):
    client, game, workspace = Client(replies), Game(), Workspace()
    runner = LearningLoop(client, game, workspace, tmp_path, **kwargs)
    return runner, client, game, workspace


def test_raw_start_learns_writes_resets_and_reads_own_memory(tmp_path):
    runner, client, game, workspace = loop(tmp_path, [
        call("game_observe"),
        call("workspace_write", path="my-notes.md", content="I observed Urist with stress 7."),
        call("context_reset", keep="Read my-notes.md."),
        call("workspace_read", path="my-notes.md"),
        call("game_wait", ticks=42), call("finish"),
    ])
    assert workspace.files == {}
    result = runner.run()
    assert result["error"] is None and result["pause_confirmed"]
    assert game.ticks == 42 and result["calls"] == 6
    initial = client.requests[0]
    assert initial[1]["content"] == OBJECTIVE
    assert "brew" not in initial[0]["content"].lower()
    assert "my-notes.md" not in json.dumps(initial)
    after_reset = client.requests[3]
    assert not any('"stress":7' in message["content"] for message in after_reset)
    assert any(message["content"] == "Read my-notes.md." for message in after_reset)
    assert any("Urist with stress 7" in message["content"] for message in client.requests[4])
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "session_closed"


def test_shared_inference_uses_model_messages_and_the_same_call_limit(tmp_path):
    custom = [{"role": "user", "content": "A context I chose myself"}]
    runner, client, game, _ = loop(tmp_path, [call("infer", messages=custom, max_output_tokens=100), "a reply"],
                                limits=PlayLimits(max_calls=2))
    result = runner.run()
    assert client.requests[1] == custom
    assert result["calls"] == 2 and "call limit" in result["stop_reason"]
    assert game.ticks == 0


def test_invalid_output_is_visible_then_model_can_choose_again(tmp_path):
    runner, client, game, _ = loop(tmp_path, [{"shell": "something"}, call("game_wait", ticks=0), call("finish")])
    assert runner.run()["error"] is None
    assert "Expected exactly tool" in client.requests[1][-1]["content"]
    assert "ticks in 1..12000" in client.requests[2][-1]["content"]
    assert game.ticks == 0


def test_model_authored_routine_executes_bounded_steps_with_evidence(tmp_path):
    routine = [call("game_wait", ticks=5), call("game_observe")]
    runner, client, game, _ = loop(tmp_path, [
        call("workspace_write", path="routine.json", content=json.dumps(routine)),
        call("workspace_run", path="routine.json"), call("finish"),
    ])
    assert runner.run()["error"] is None and game.ticks == 5
    assert '"routine_result"' in (tmp_path / "events.jsonl").read_text()


def test_routine_cannot_reset_context_or_spawn_inference(tmp_path):
    routine = [call("game_wait", ticks=5), call("infer", messages=[], max_output_tokens=10)]
    runner, client, game, _ = loop(tmp_path, [
        call("workspace_write", path="routine.json", content=json.dumps(routine)),
        call("workspace_run", path="routine.json"), call("finish"),
    ])
    assert runner.run()["error"] is None
    assert game.ticks == 0  # entire control-tool validation precedes mutation


def test_provider_failure_stops_without_retry_and_pauses(tmp_path):
    failure = TextInferenceError("transport failed", exchange={"provider_may_be_running": True})
    runner, client, game, _ = loop(tmp_path, [failure])
    assert runner.run()["error"] == "transport failed"
    assert len(client.requests) == 1 and game.paused


def test_tiny_budget_prevents_first_paid_call(tmp_path):
    account = SpendAccount("0.00001", 1, 5, cloud=True)
    runner, client, game, _ = loop(tmp_path, [], account=account)
    result = runner.run()
    assert "Spending limit" in result["stop_reason"]
    assert client.requests == [] and game.paused


def test_spending_reservation_refunds_only_reported_usage():
    account = SpendAccount(10, 2, 8, cloud=True)
    reserved = account.reserve(10000, 4096)
    assert reserved == Decimal("0.060960")
    account.settle(reserved, {"prompt_tokens": 100, "completion_tokens": 50})
    assert account.charged == Decimal("0.0006")
    reserved = account.reserve(10000, 4096)
    account.settle(reserved, {"prompt_tokens": 100})
    assert account.charged == Decimal("0.061560")
    with pytest.raises(LimitReached, match="uncertain"):
        account.reserve(1, 1)


@pytest.mark.parametrize("value", ["NaN", "Infinity", -1, True])
def test_invalid_price_is_not_an_unlimited_budget(value):
    with pytest.raises(ValueError):
        SpendAccount(10, value, 1, cloud=True)


def test_stop_file_and_tick_limit_prevent_game_changes(tmp_path):
    (tmp_path / "STOP").touch()
    runner, client, game, _ = loop(tmp_path, [])
    assert "STOP file" in runner.run()["stop_reason"]
    assert client.requests == [] and game.ticks == 0


def test_context_limit_stops_before_provider(tmp_path):
    runner, client, game, _ = loop(tmp_path, [], limits=PlayLimits(context_bytes=10))
    assert "Context is full" in runner.run()["stop_reason"]
    assert client.requests == [] and game.paused
