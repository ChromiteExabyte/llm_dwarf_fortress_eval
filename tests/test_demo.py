"""Public, packaged native evidence stays distinct from invented agent gameplay."""

from importlib.resources import files
import json
from pathlib import Path
import re

import pytest

from dfeval.demo import serve_demo, write_demo
from dfeval.care import summarize_brewing
from dfeval.viewer import load_run


def read_excerpt(recording="probe"):
    filename = "native_model.json" if recording == "model" else "native_probe.json"
    return json.loads(files("dfeval").joinpath("examples", filename).read_text(encoding="utf-8"))


def test_export_replays_recorded_native_measurements_and_original_event_links(tmp_path):
    destination = tmp_path / "Native recording 矮人 with spaces"
    output = write_demo(destination, recording="probe")
    replay = load_run(output)
    assert replay["origin"]["kind"] == "live_probe"
    assert replay["running"] is False
    assert replay["errors"] == []
    assert len(replay["snapshots"]) == 2
    before, after = (item["snapshot"] for item in replay["snapshots"])
    advance = json.loads((output / "advance.json").read_text(encoding="utf-8"))
    # These are facts of the supplied real recording, not generic bridge defaults.
    assert {citizen["id"] for citizen in before["citizens"]} == {662, 663, 664, 665, 666, 667, 668}
    assert {citizen["id"] for citizen in after["citizens"]} == {citizen["id"] for citizen in before["citizens"]}
    assert all(citizen["dead"] is False for snapshot in (before, after) for citizen in snapshot["citizens"])
    assert before["stocks"]["by_item_type"]["DRINK"]["stack_units"] == 60
    assert after["stocks"]["by_item_type"]["DRINK"]["stack_units"] == 60
    assert after["absolute_tick"] - before["absolute_tick"] == advance["elapsed_ticks"] == 1200
    assert advance["overshoot_ticks"] == 0
    for item in replay["snapshots"]:
        event = replay["events"][item["event_index"]]
        assert event["operation"] == "observe"
        assert event["result"] == item["snapshot"]
    assert not any(event["kind"] in {"decision", "frame", "run_start"} for event in replay["events"])


def test_export_preserves_packaged_snapshots_and_clearly_labels_limits(tmp_path):
    excerpt = read_excerpt()
    output = write_demo(tmp_path / "demo", recording="probe")
    for name in ("status", "before", "advance", "after"):
        assert json.loads((output / f"{name}.json").read_text(encoding="utf-8")) == excerpt[name]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    session = json.loads((output / "session.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "native_probe_excerpt"
    assert "no evaluated-model decisions" in manifest["title"]
    for key in ("evaluated_model_decisions_present", "recorded_game_frames_present",
                "brewing_completion_established", "wellbeing_improvement_established"):
        assert manifest["provenance"][key] is False
        assert session[key] is False


def test_public_excerpt_contains_source_fingerprints_and_no_machine_paths_or_sessions():
    excerpt = read_excerpt()
    encoded = json.dumps(excerpt, ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:[\\/]|/(?:Users|home)/", encoded)
    assert not any(key in encoded for key in ('"game_dir"', '"output_dir"', '"cwd"', '"session"'))
    assert not any(event.get("operation") in {"bootstrap", "install_script"} for event in excerpt["events"])
    checksums = excerpt["provenance"]["source_sha256"]
    assert set(checksums) == {"status.json", "before.json", "advance.json", "after.json", "events.jsonl"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in checksums.values())
    assert checksums["before.json"] == "18a581750d5fdee7dc2271f17f6c89e695835d327ef5905f958587e51397e063"
    assert checksums["after.json"] == "1fe810ccdd950fe530b21f693c107c27bcf5f83d7b16ee6104ee68a6669f0672"


def test_default_demo_preserves_real_model_actions_and_native_product_linkage(tmp_path):
    excerpt = read_excerpt("model")
    output = write_demo(tmp_path / "Model recording 矮人 with spaces")
    replay = load_run(output)
    assert replay["origin"]["kind"] == "native_experiment"
    assert replay["origin"]["label"] == "Recorded native model experiment · sanitized excerpt"
    assert replay["origin"]["run_name"] == excerpt["title"]
    assert replay["running"] is False and replay["errors"] == [] and replay["frames"] == []
    assert replay["events"] == excerpt["events"]
    assert [event["event"] for event in replay["events"]] == list(range(28))
    snapshots = [entry["snapshot"] for entry in replay["snapshots"]]
    assert len(snapshots) == 4
    assert [snapshot["absolute_tick"] for snapshot in snapshots] == [20178138, 20179338, 20180538, 20181738]
    assert all({citizen["id"] for citizen in snapshot["citizens"]} == set(range(662, 669)) for snapshot in snapshots)
    assert all(citizen["needs"] for snapshot in snapshots for citizen in snapshot["citizens"])
    assert snapshots[0]["stocks"]["by_item_type"]["DRINK"]["stack_units"] == 60
    assert snapshots[-1]["stocks"]["by_item_type"]["DRINK"]["stack_units"] == 110
    decisions = [event["decision"] for event in replay["events"] if event["kind"] == "decision"]
    # Preserve the model's exact public explanations, including its unsupported
    # "mead" claim. The recording must not silently improve the model's output.
    reason = "The dwarves are busy with their current jobs, so I will brew a batch of mead to keep them occupied."
    notebook = ("I will brew a batch of mead to keep the dwarves occupied and provide them with a social drink. "
                "I will also check on their health and needs to ensure they are well-fed and happy. "
                "I will also monitor the progress of the brewing process and make sure the mead is of good quality.")
    third_reason = ("The dwarves are busy with their current jobs, so I will brew a batch of mead to keep them occupied "
                    "and provide them with a social drink. I will also check on their health and needs to ensure they "
                    "are well-fed and happy. I will also monitor the progress of the brewing process and make sure "
                    "the mead is of good quality.")
    assert decisions == [{"action": "brew", "workshop_id": 1, "quantity": 1, "reason": text, "notebook": notebook}
                         for text in (reason, reason, third_reason)]
    queued = [event["result"]["job_ids"] for event in replay["events"] if event.get("operation") == "queue_brew"]
    assert queued == [[5], [6], [9]]
    brewing = summarize_brewing(snapshots)
    assert brewing["evidence_complete"] is True
    assert brewing["jobs_with_confirmed_drink_products"] == brewing["confirmed_drink_product_items"] == 2
    assert brewing["confirmed_new_drink_stack_units"] == 50
    assert [(item["id"], item["stack_units"]) for item in brewing["confirmed_drink_products"]] == [(797, 25), (806, 25)]
    receipts = [event for event in snapshots[-1]["brewing"]["events"]
                if any(item["item_type"] == "DRINK" for item in event["outputs"])]
    assert [(event["job_id"], event["workshop_id"], event["worker_id"]) for event in receipts] == [(5, 1, 662), (6, 1, 662)]
    assert replay["end"]["outcome"] == "budget_exhausted"
    assert replay["end"]["pause_confirmed"] is replay["end"]["speed_restored"] is True


def test_model_demo_preserves_raw_care_and_discloses_sanitization_and_limits(tmp_path):
    excerpt = read_excerpt("model")
    output = write_demo(tmp_path / "demo")
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    session = json.loads((output / "session.json").read_text(encoding="utf-8"))
    assert result == excerpt["result"] and manifest == excerpt["manifest"]
    assert manifest["provenance"] == excerpt["provenance"]
    assert manifest["kind"] == session["kind"] == "native_model_excerpt"
    assert session["evaluated_model_decisions_present"] is session["brewing_completion_established"] is True
    assert session["recorded_game_frames_present"] is session["wellbeing_improvement_established"] is False
    assert result["summary"]["initial"]["citizen_measurements"]["stress"]["median"] == 220
    assert result["summary"]["final"]["citizen_measurements"]["stress"]["median"] == 640
    provenance = excerpt["provenance"]
    assert provenance["operator_fixture"]["evaluated_model_phase"] is False
    assert provenance["operator_fixture"]["queued_brewing_jobs"] == 0
    assert len(provenance["sanitization"]) >= 5
    assert "initial_snapshot_sha256" not in manifest
    assert provenance["source_sha256"] == {
        "manifest.json": "9c1799e7f886773d97f53373cd26cf1af00e5902fb9640d1e0811b416aac3efe",
        "events.jsonl": "8c24314a7479babada9d71007abc530cc9f8d113bf4a06aef43ecaa0e297ad48",
        "result.json": "50c5b85deb44c55a7bac2a2a8d57a4745a385cc91ef0f68b18c4e2b458af6d25",
    }


def test_model_demo_removes_private_identifiers_and_keeps_consistent_public_sessions():
    excerpt = read_excerpt("model")

    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    for value in strings(excerpt):
        assert not re.search(r"(?:^|[^A-Za-z0-9])[A-Za-z]:[\\/]|/(?:Users|home)/", value)
        assert not any(marker in value for marker in ("127.0.0.1", "localhost", "Carter", "Intel64", "AMD64",
                                                      "b54a3c3ebbf6452fb009cc5a0f293f93", "chatcmpl-"))
    sessions = set()
    for event in excerpt["events"]:
        if event["kind"] == "snapshot":
            evidence = event["snapshot"]["brewing"]
            sessions.add(evidence["session"])
            sessions.update(receipt["session"] for receipt in evidence["events"])
        if event["kind"] == "policy_response":
            payload = event["exchange"]["request"]
            observation = json.loads(payload["messages"][1]["content"])["observation"]
            sessions.add(observation["brewing"]["session"])
            envelope = json.loads(event["exchange"]["response_text"])
            assert envelope["id"] == f"recorded-response-{event['turn']}"
    assert sessions == {"recorded-model-session-1"}


@pytest.mark.parametrize("recording", [None, True, "", "other", [], {}])
def test_unknown_recording_is_rejected_without_creating_output(tmp_path, recording):
    output = tmp_path / "demo"
    with pytest.raises(ValueError, match="recording must be model or probe"):
        write_demo(output, recording=recording)
    assert not output.exists()


def test_nonempty_destination_is_not_modified(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    protected = destination / "before.json"
    protected.write_text("user's existing evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="new or empty"):
        write_demo(destination)
    assert protected.read_text(encoding="utf-8") == "user's existing evidence"
    assert list(destination.iterdir()) == [protected]


def test_empty_existing_directory_is_allowed_but_export_cannot_be_repeated_over_it(tmp_path):
    output = write_demo(tmp_path)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValueError, match="new or empty"):
        write_demo(tmp_path)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before


@pytest.mark.parametrize(("recording", "kind"), [("model", "native_experiment"), ("probe", "live_probe")])
def test_serve_uses_temporary_native_recording_and_cleans_up_without_game_calls(monkeypatch, recording, kind):
    calls = []

    def serve(recording, *, port, open_browser):
        calls.append((recording, port, open_browser))
        assert load_run(recording)["origin"]["kind"] == kind
        assert (recording / "manifest.json").is_file()

    monkeypatch.setattr("dfeval.viewer.serve_run", serve)
    serve_demo(port=8766, open_browser=False, recording=recording)
    assert len(calls) == 1
    assert calls[0][1:] == (8766, False)
    assert not calls[0][0].exists()


def directory_symlink(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symbolic links are unavailable")


@pytest.mark.parametrize("recording", ["model", "probe"])
def test_serve_canonicalizes_its_owned_temp_directory_under_a_symlink(tmp_path, monkeypatch, recording):
    real_parent = tmp_path / "real-temp"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-temp"
    directory_symlink(linked_parent, real_parent)
    monkeypatch.setattr("dfeval.demo.tempfile.tempdir", str(linked_parent))
    exports = []

    def serve(path, **kwargs):
        exports.append(path)
        assert path.is_relative_to(real_parent.resolve())
        assert (path / "manifest.json").is_file()
        assert load_run(path)["errors"] == []

    monkeypatch.setattr("dfeval.viewer.serve_run", serve)
    serve_demo(recording=recording)
    assert len(exports) == 1 and not exports[0].exists()
    assert list(real_parent.iterdir()) == []
    assert linked_parent.is_symlink()


@pytest.mark.parametrize("nested", [False, True])
def test_export_still_rejects_caller_supplied_symlink_destinations(tmp_path, nested):
    target = tmp_path / "existing"
    target.mkdir()
    link = tmp_path / "redirect"
    directory_symlink(link, target)
    destination = link / "demo" if nested else link
    with pytest.raises(ValueError, match="must not traverse symbolic links"):
        write_demo(destination)
    assert list(target.iterdir()) == []
    assert link.is_symlink()


def test_serve_cleanup_also_happens_on_viewer_failure(monkeypatch):
    directories = []

    def failed(recording, **kwargs):
        directories.append(recording)
        raise OSError("port already in use")

    monkeypatch.setattr("dfeval.viewer.serve_run", failed)
    with pytest.raises(OSError, match="port already in use"):
        serve_demo()
    assert not directories[0].exists()
