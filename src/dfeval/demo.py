"""Offline spectator demos made from actual recorded native-game evidence.

Only packaged, reviewed evidence is used. These helpers never instantiate a
game bridge, run DFHack, invoke a model, or synthesize decisions or screenshots.
"""

from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
import tempfile
from typing import Any


RECORDINGS = {"model": ("native_model.json", "native_model_excerpt"),
              "probe": ("native_probe.json", "native_probe_excerpt")}


def _load_demo(recording: str) -> dict[str, Any]:
    if not isinstance(recording, str) or recording not in RECORDINGS:
        raise ValueError("recording must be model or probe")
    filename, kind = RECORDINGS[recording]
    resource = files("dfeval").joinpath("examples", filename)
    excerpt = json.loads(resource.read_text(encoding="utf-8"))
    if (excerpt.get("schema_version") != 1 or excerpt.get("kind") != kind
            or not isinstance(excerpt.get("events"), list)):
        raise ValueError("Packaged native recording is malformed")
    required = ("manifest", "result", "provenance") if recording == "model" else (
        "status", "before", "advance", "after", "provenance")
    for name in required:
        if not isinstance(excerpt.get(name), dict):
            raise ValueError(f"Packaged native recording is missing {name}")
    return excerpt


def write_demo(destination: str | Path, *, recording: str = "model") -> Path:
    """Export recorded evidence into a new or empty spectator directory.

    The export retains original native values and sanitized original events.
    Provenance records hashes of the original source files; exported JSON
    formatting may differ. Existing files are never overwritten.
    """
    requested = Path(destination).expanduser().absolute()
    for component in (requested, *requested.parents):
        if component.is_symlink():
            raise ValueError("Demo destination must not traverse symbolic links")
    if requested.exists() and (not requested.is_dir() or any(requested.iterdir())):
        raise ValueError("Demo destination must be a new or empty directory")
    excerpt = _load_demo(recording)
    requested.mkdir(parents=True, exist_ok=True)
    target = requested.resolve()
    provenance = excerpt["provenance"]
    if recording == "model":
        manifest = excerpt["manifest"]
        outputs = {"result.json": excerpt["result"]}
        label = "Completed local-model recording; native brewing confirmed; improved wellbeing not established"
    else:
        manifest = {"schema_version": 1, "kind": excerpt["kind"], "title": excerpt["title"],
                    "mode": "recorded_evidence_only", "provenance": provenance}
        outputs = {f"{name}.json": excerpt[name] for name in ("status", "before", "advance", "after")}
        label = "Completed native-game recording; no evaluated-model decisions"
    session = {"schema_version": 1, "kind": excerpt["kind"], "label": label,
               "mode": "recorded_evidence_only", **{name: provenance[name] for name in (
                   "evaluated_model_decisions_present", "recorded_game_frames_present",
                   "brewing_completion_established", "wellbeing_improvement_established")}}
    outputs.update({"manifest.json": manifest, "session.json": session})
    for name, value in outputs.items():
        # Exclusive creation also refuses a file created by another writer
        # after the empty-directory check; it never silently replaces data.
        with (target / name).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    with (target / "events.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for event in excerpt["events"]:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    return target


def serve_demo(port: int = 8765, open_browser: bool = False, *, recording: str = "model") -> None:
    """Serve a temporary recording until interrupted, then remove the export.

    A browser is opened only when explicitly requested. No game is required.
    """
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Port must be an integer from 0 to 65535")
    if type(open_browser) is not bool:
        raise ValueError("open_browser must be a boolean")
    from .viewer import serve_run

    with tempfile.TemporaryDirectory(prefix="dfeval-recorded-evidence-") as temporary:
        exported = write_demo(Path(temporary) / ("native-model-brewing" if recording == "model" else "native-probe"),
                              recording=recording)
        if recording == "model":
            print("Recorded local Qwen2.5 model: three brewing decisions, two native drink products totaling 50 units.")
            print("Sanitized original evidence, with no game video. Improved wellbeing is not established.")
        else:
            print("Recorded native Dwarf Fortress probe: no evaluated-model decisions or game video.")
            print("This offline replay does not demonstrate improved wellbeing or completed brewing.")
        serve_run(exported, port=port, open_browser=open_browser)
