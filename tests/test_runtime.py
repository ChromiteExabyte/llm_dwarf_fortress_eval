import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dfeval.runtime import RuntimeBroker, RuntimeLimits, RuntimeRejected
from dfeval.runtime_witness import WitnessError, verify_witness


class Client:
    max_response_bytes = 4096
    max_output_tokens = 2048
    timeout = 5

    def __init__(self, *, completion_tokens=3):
        self.requests = []
        self.completion_tokens = completion_tokens
        self.started = threading.Event()
        self.release = None

    def public_config(self):
        return {"model": "fixed-model", "provider": "test-double"}

    def check_public_input(self, value):
        pass

    def infer(self, request, *, timeout=None):
        self.requests.append(request.to_dict())
        self.started.set()
        if self.release:
            assert self.release.wait(5)
        text = request.messages[-1]["content"]
        usage = {"completion_tokens": self.completion_tokens, "prompt_tokens": 9, "cached_prompt_tokens": None}
        return {"text": text, "finish_reason": "stop", "usage": usage, "timing": {},
                "exchange": {"request": request.to_dict(), "response_text": json.dumps(text),
                             "usage": usage}}


def request(text="a", tokens=10):
    return {"messages": [{"role": "user", "content": text}], "max_output_tokens": tokens, "temperature": None}


def events(broker):
    return [json.loads(line) for line in (broker.out / "evidence/events.jsonl").read_text().splitlines()]


def test_fresh_contexts_share_resources_without_injected_roles_or_history(tmp_path):
    client = Client()
    broker = RuntimeBroker(tmp_path / "run", client, limits=RuntimeLimits(max_output_tokens=12))
    first = request("first independent context", tokens=10)
    saved = copy.deepcopy(first)
    assert broker.infer(first)["text"] == first["messages"][0]["content"]
    first["messages"][0]["content"] = "changed afterwards"
    second = request("second independent context", tokens=9)
    assert broker.infer(second)["text"] == second["messages"][0]["content"]
    assert client.requests == [saved, second]
    assert broker.status()["output_tokens_charged_or_reserved"] == 6
    assert list(broker.workspace.iterdir()) == []
    finished = broker.close()
    audit = verify_witness(broker.out / "evidence/events.jsonl", expected_head=finished["witness_head_sha256"])
    assert audit["records"] == 6 and audit["has_close_record"]
    assert events(broker)[1]["data"]["request"] == saved
    with pytest.raises(FileExistsError):
        RuntimeBroker(broker.out, client)


def test_concurrent_contexts_cannot_overdraw_last_reservation(tmp_path):
    client = Client()
    client.release = threading.Event()
    broker = RuntimeBroker(tmp_path / "run", client, limits=RuntimeLimits(max_output_tokens=10))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(broker.infer, request())
        assert client.started.wait(2)
        with pytest.raises(RuntimeRejected, match="allowance exhausted"):
            pool.submit(broker.infer, request("other context")).result(timeout=2)
        assert len(client.requests) == 1
        assert broker.status()["output_tokens_charged_or_reserved"] == 10
        client.release.set()
        assert first.result(timeout=2)["call_id"] == 1
    broker.close()


def test_call_limit_applies_even_after_token_refund(tmp_path):
    client = Client(completion_tokens=0)
    broker = RuntimeBroker(tmp_path / "run", client, limits=RuntimeLimits(max_calls=1))
    broker.infer(request())
    with pytest.raises(RuntimeRejected) as caught:
        broker.infer(request())
    assert caught.value.code == "resource_limit" and len(client.requests) == 1
    broker.close()


def test_unknown_usage_retains_output_cap(tmp_path):
    broker = RuntimeBroker(tmp_path / "run", Client(completion_tokens=None))
    broker.infer(request())
    status = broker.close()
    assert status["output_tokens_charged_or_reserved"] == 10
    assert status["calls_with_unknown_usage"]["completion_tokens"] == 1
    assert status["reported_usage_totals"]["completion_tokens"] == 0


def test_timeout_closes_admission_and_never_delivers_late_output(tmp_path):
    client = Client()
    client.release = threading.Event()
    broker = RuntimeBroker(tmp_path / "run", client, limits=RuntimeLimits(call_timeout=0.05))
    with pytest.raises(RuntimeRejected) as caught:
        broker.infer(request())
    assert caught.value.code == "timeout"
    assert broker.status()["output_tokens_charged_or_reserved"] == 10
    with pytest.raises(RuntimeRejected) as caught:
        broker.infer(request())
    assert caught.value.code == "closed"
    client.release.set()
    broker.close()
    assert events(broker)[2]["data"]["outcome"] == "timeout"
    assert events(broker)[-1]["data"]["provider_activity_may_continue"]
    assert len(client.requests) == 1


def test_close_abandons_wait_without_releasing_unknown_reservation(tmp_path):
    client = Client()
    client.release = threading.Event()
    broker = RuntimeBroker(tmp_path / "run", client)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(broker.infer, request())
        assert client.started.wait(2)
        broker.close()
        with pytest.raises(RuntimeRejected) as caught:
            future.result(timeout=2)
        assert caught.value.code == "abandoned"
    client.release.set()
    assert broker.status()["output_tokens_charged_or_reserved"] == 10
    assert [e["kind"] for e in events(broker)][-2:] == ["inference_finished", "session_closed"]


@pytest.mark.parametrize("when", ["before_dispatch", "before_delivery"])
def test_witness_failure_prevents_dispatch_or_delivery_and_closes_admission(tmp_path, monkeypatch, when):
    client = Client()
    broker = RuntimeBroker(tmp_path / "run", client)
    original = broker._witness.record

    def fail(kind, data):
        if kind == ("inference_reserved" if when == "before_dispatch" else "inference_finished"):
            raise WitnessError("simulated storage failure")
        return original(kind, data)

    monkeypatch.setattr(broker._witness, "record", fail)
    with pytest.raises(WitnessError):
        broker.infer(request())
    assert len(client.requests) == (0 if when == "before_dispatch" else 1)
    with pytest.raises(RuntimeRejected) as caught:
        broker.infer(request())
    assert caught.value.code == "closed"
    broker.close()


def test_provider_usage_above_cap_is_not_refunded_or_delivered(tmp_path):
    broker = RuntimeBroker(tmp_path / "run", Client(completion_tokens=11))
    with pytest.raises(RuntimeRejected) as caught:
        broker.infer(request())
    assert caught.value.code == "invalid_usage"
    assert broker.status()["output_tokens_charged_or_reserved"] == 10
    broker.close()


def test_utf8_request_size_limit_and_unknown_fields_reject_before_dispatch(tmp_path):
    client = Client()
    broker = RuntimeBroker(tmp_path / "run", client, limits=RuntimeLimits(max_request_bytes=150))
    for value in (request("\U0001f600" * 40), {**request(), "model": "another"},
                  {**request(), "agent": "invented required identity"}):
        with pytest.raises(RuntimeRejected):
            broker.infer(value)
    assert client.requests == []
    broker.close()


def test_insufficient_evidence_capacity_rejects_before_dispatch(tmp_path):
    client = Client()
    broker = RuntimeBroker(tmp_path / "run", client, limits=RuntimeLimits(max_evidence_bytes=65536))
    with pytest.raises(RuntimeRejected) as caught:
        broker.infer(request())
    assert caught.value.code == "evidence_limit" and client.requests == []
    broker.close()


def test_witness_tampering_and_truncation_against_retained_head(tmp_path):
    broker = RuntimeBroker(tmp_path / "run", Client())
    broker.infer(request("original"))
    head = broker.close()["witness_head_sha256"]
    path = broker.out / "evidence/events.jsonl"
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"original", b"modified"))
    with pytest.raises(ValueError, match="mismatch"):
        verify_witness(path)
    path.write_bytes(b"\n".join(raw.splitlines()[:-1]) + b"\n")
    assert not verify_witness(path)["has_close_record"]
    with pytest.raises(ValueError, match="retained head"):
        verify_witness(path, expected_head=head)


@pytest.mark.parametrize("limits", [{"max_calls": True}, {"max_wall_seconds": float("nan")},
                                    {"max_in_flight": 1000}, {"max_evidence_bytes": 42}])
def test_bad_limits_rejected(limits):
    with pytest.raises(ValueError):
        RuntimeLimits(**limits)
