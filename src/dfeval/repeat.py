"""Explicit, two-phase repeats from verified local starting saves.

Preparation restores only through scenario.restore_save, preserving any current
save. Loading a recipe never restores, starts a game, or calls a model. The
checksum detects accidental changes; it does not authenticate an untrusted author.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from typing import Any
import uuid

from . import comparison, scenario
from .doctor import inspect_installation
from .experiment import ExperimentConfig
from .environment import capture_environment, recorded_environment_fingerprint
from .live import PROTOCOL_VERSION
from .local_models import OllamaPolicy
from .model_observation import MODEL_OBSERVATION_VERSION, projection_contract
from .policies import ChatCompletionsPolicy, IdlePolicy, RulePolicy, json_bytes


PLAN_LIMIT = 4 * 1024 * 1024
INITIAL_LIMIT = 16 * 1024 * 1024
_CONFIG_KEYS = {field.name for field in fields(ExperimentConfig)}
_PLAN_KEYS = {"schema_version", "kind", "created_at", "model", "config", "policy_config",
              "initial_expectation", "source", "paths", "restore_receipt", "seal_sha256"}


class RepeatError(ValueError):
    """A repeat cannot preserve or verify the declared source conditions."""


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _read(path: Path, limit: int) -> bytes:
    checked = scenario._path(path)
    return comparison._read(checked, limit)


def _json(raw: bytes) -> Any:
    try:
        return comparison._evidence_json(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RepeatError("Repeat evidence contains malformed or excessively nested JSON") from exc


def _digest(value: Any, label: str) -> str:
    digest = comparison._digest(value)
    if digest is None or value != digest:
        raise RepeatError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _relative(path: Path, root: Path) -> str:
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:  # Different Windows volumes cannot have a relative path.
        return str(path)


def _resolve(value: Any, root: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RepeatError("Recipe path must be a nonempty local path")
    path = Path(value).expanduser()
    return scenario._path(path if path.is_absolute() else root / path)


def _bridge_hash() -> str:
    return _hash(_read(Path(__file__).parent / "bridge" / "lua" / "dfeval-live.lua", PLAN_LIMIT))


def _clone_policy(config: Any, model: str | None = None):
    if not isinstance(config, dict) or config.get("is_model") is not True:
        raise RepeatError("Repeat requires a recorded model policy, not a scripted baseline")
    desired = copy.deepcopy(config)
    if model is not None:
        desired["model"] = model
        if desired.get("kind") == "ollama":
            desired["model_identity"] = None  # A previous model's digest never identifies the new model.
    common = ("model", "api_key_env", "max_completion_tokens", "timeout",
              "max_response_bytes", "max_request_bytes", "system_prompt")
    try:
        args = {key: desired[key] for key in common}
        if desired.get("kind") == "ollama":
            options = desired["options"]
            policy = OllamaPolicy(**args, base_url=desired["base_url"],
                                  num_ctx=options["num_ctx"], temperature=options["temperature"],
                                  seed=options["seed"], keep_alive=desired["keep_alive"],
                                  think=desired["think"], model_identity=desired["model_identity"])
        elif desired.get("kind") == "chat_completions":
            policy = ChatCompletionsPolicy(**args, mode=desired["mode"], endpoint=desired["endpoint"],
                                           token_limit_field=desired["token_limit_field"],
                                           response_format=desired["response_format"]["type"])
        else:
            raise RepeatError("Only recorded Ollama and Chat Completions model policies can be repeated")
    except (KeyError, TypeError, ValueError) as exc:
        raise RepeatError(f"Recorded model configuration cannot be reconstructed: {exc}") from exc
    if policy.public_config() != desired:
        raise RepeatError("Recorded policy settings, system prompt, or response schema differ from this installed version")
    return policy


def policy_from_plan(plan: dict[str, Any]):
    """Reconstruct the sealed model settings without network calls or defaults.

    Call load_repeat immediately before execution. It verifies the complete
    recipe and local evidence; this helper only reconstructs the policy.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("policy_config"), dict):
        raise RepeatError("Recipe policy configuration must be an object")
    if plan.get("kind") == "native_control_repeat":
        if plan.get("model") is not None:
            raise RepeatError("A control recipe cannot select a model")
        policy = _control_policy(plan.get("policy_config", {}).get("kind"))
        if policy.public_config() != plan.get("policy_config"):
            raise RepeatError("Control settings differ from the declared baseline")
        return policy
    if plan.get("model") != plan["policy_config"].get("model"):
        raise RepeatError("Recipe model and policy configuration disagree")
    return _clone_policy(plan["policy_config"])


def _control_policy(kind: str):
    if kind == "idle":
        return IdlePolicy()
    if kind == "rule":
        return RulePolicy()
    raise RepeatError("Control must be idle or rule")


def _config(manifest: dict[str, Any]) -> dict[str, Any]:
    config = manifest.get("config")
    if not isinstance(config, dict) or set(config) != _CONFIG_KEYS | {"policy", "scenario", "model_observation_version"}:
        raise RepeatError("Source must record every current ExperimentConfig field without unknown settings")
    if (manifest.get("model_observation_version") != MODEL_OBSERVATION_VERSION or
            manifest.get("model_observation") != projection_contract() or
            config.get("model_observation_version") != MODEL_OBSERVATION_VERSION or
            not isinstance(manifest.get("policy"), dict) or
            manifest["policy"].get("model_observation_version") != MODEL_OBSERVATION_VERSION):
        raise RepeatError("Source model observation version differs from this installed version; record a new source run")
    if config["scenario"] != "drink-maintenance-v1" or config["policy"] != manifest.get("policy"):
        raise RepeatError("Source scenario or policy provenance does not match the current interface")
    values = {key: config[key] for key in _CONFIG_KEYS}
    try:
        return asdict(ExperimentConfig(**values))
    except (TypeError, ValueError) as exc:
        raise RepeatError(f"Source experiment settings are invalid: {exc}") from exc


def _expectation(manifest: dict[str, Any], initial: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("protocol") != PROTOCOL_VERSION or type(manifest.get("protocol")) is not int:
        raise RepeatError("Source bridge protocol differs from this installed version")
    if manifest.get("bridge_sha256") != _bridge_hash():
        raise RepeatError("Source bridge hash differs from the currently shipped script; use the matching project version")
    if manifest.get("initial_snapshot_sha256") != _hash(json_bytes(initial)):
        raise RepeatError("Source initial snapshot integrity hash does not match")
    if not comparison.NATIVE_FIELDS <= initial.keys():
        raise RepeatError("Source initial snapshot is not a native fortress observation")
    if any(initial.get(key) is not True for key in ("world_loaded", "map_loaded", "fortress_mode", "paused")):
        raise RepeatError("Source initial observation must confirm a paused loaded fortress")
    name = scenario._save_name(initial.get("save_directory"))
    versions = {key: initial.get(key) for key in ("df_version", "dfhack_version")}
    if any(not isinstance(value, str) or not value.strip() for value in versions.values()):
        raise RepeatError("Source must record native game and DFHack versions")
    if manifest.get("game_environment") is None:
        raise RepeatError("Source has no recorded installation environment; its original external settings cannot be reconstructed. Record a new source run with this version")
    environment_hash = recorded_environment_fingerprint(manifest["game_environment"])
    return {"initial_observation_sha256": comparison.initial_observation_fingerprint(initial),
            "save_directory": name, **versions,
            "bridge_sha256": manifest["bridge_sha256"], "protocol": manifest["protocol"],
            "game_environment_sha256": environment_hash}


def _source(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bytes, bytes]:
    manifest_raw = _read(root / "manifest.json", PLAN_LIMIT)
    manifest = _json(manifest_raw)
    if not isinstance(manifest, dict):
        raise RepeatError("Source manifest must be a JSON object")
    if not isinstance(manifest.get("config"), dict) or manifest["config"].get("starting_save_sha256") is None:
        raise RepeatError("Source has no declared starting snapshot; a post-run save cannot reconstruct its start. Capture a starting snapshot before a new source run")
    events_raw = _read(root / "events.jsonl", comparison.MAX_LOG_BYTES)
    validated = comparison.read_run(root)
    if manifest_raw != _read(root / "manifest.json", PLAN_LIMIT) or events_raw != _read(root / "events.jsonl", comparison.MAX_LOG_BYTES):
        raise RepeatError("Source recording changed while being inspected; wait for it to finish")
    if not validated["complete_record"] or not validated["world_time_continuity_verified"]:
        raise RepeatError("Repeat requires a complete coherent recording with verified native world/time continuity")
    # Model errors are legitimate source outcomes. Evidence/provenance failures are not.
    allowed_warning = "The event stream records an action, run, or cleanup failure."
    rejected = [warning for warning in validated["warnings"]
                if warning != allowed_warning and not warning.startswith("Terminal outcome is ")]
    if rejected:
        raise RepeatError("Source evidence cannot support a repeat: " + " ".join(rejected))
    events = [_json(line) for line in events_raw.splitlines() if line.strip()]
    initial = next(event["snapshot"] for event in events if event.get("kind") == "snapshot")
    return manifest, initial, validated, manifest_raw, events_raw


def _snapshot(path: Path, expected_hash: str, expectation: dict[str, Any]) -> dict[str, Any]:
    manifest = scenario.verify_snapshot(path)
    if _hash(_read(path / "manifest.json", 16 * 1024 * 1024)) != expected_hash:
        raise RepeatError("Starting snapshot does not match the source run's declared starting-save SHA-256")
    if manifest["save_name"] != expectation["save_directory"]:
        raise RepeatError("Starting snapshot save name differs from the source native save-directory identity")
    if not scenario.compatible_df_versions(manifest["df_version"], expectation["df_version"]):
        raise RepeatError("Starting snapshot game version differs from the source native observation")
    return manifest


def _seal(plan: dict[str, Any]) -> str:
    return _hash(_canonical({key: value for key, value in plan.items() if key not in ("seal_sha256", "resolved_paths")}))


def prepare_repeat(source_run: str | Path, *, model: str | None = None, control: str | None = None,
                   output_dir: str | Path,
                   game_dir: str | Path, snapshot_dir: str | Path | None = None,
                   game_stopped: bool = False, backup_dir: str | Path | None = None,
                   save_root: str | Path | None = None) -> dict[str, Any]:
    """Validate a completed source and restore its starting snapshot with a backup.

    The caller explicitly authorizes restore by this operation and game_stopped.
    All evidence, settings, and output paths are checked before save mutation.
    The game must then be started and the restored fortress loaded manually.
    """
    if game_stopped is not True:
        raise RepeatError("Save and fully exit the game, then pass --game-stopped to prepare the repeat")
    if (model is None) == (control is None):
        raise RepeatError("Choose exactly one replacement model or an idle/rule control")
    source = scenario._directory(source_run, "Source run")
    game = scenario._directory(game_dir, "Game installation")
    explicit_save_root = scenario._save_roots(game, save_root)[0] if save_root is not None else None
    out = scenario._path(output_dir)
    if os.path.lexists(out):
        raise RepeatError("Repeat output directory must be new; existing directories are never overwritten")
    manifest, initial, validated, manifest_raw, events_raw = _source(source)
    config = _config(manifest)
    _clone_policy(manifest["policy"])
    replacement = _control_policy(control) if control is not None else _clone_policy(manifest["policy"], model)
    if control is None and replacement.model == manifest["policy"]["model"]:
        raise RepeatError("Choose a different model ID for the repeat")
    expected = _expectation(manifest, initial)
    if capture_environment(game)["fingerprint_sha256"] != expected["game_environment_sha256"]:
        raise RepeatError("Installation configuration differs from the source run; restore the original recorded settings before preparing a repeat")
    save_hash = config["starting_save_sha256"]
    if save_hash is None:
        raise RepeatError("Source has no declared starting snapshot; a post-run save cannot reconstruct its start. Capture a starting snapshot before a new source run")
    _digest(save_hash, "Starting-save identity")
    declared_path = manifest.get("starting_snapshot_path")
    if snapshot_dir is None and not declared_path:
        raise RepeatError("Starting snapshot path is unavailable; supply --starting-snapshot with the original verified snapshot. A post-run save cannot reconstruct it")
    snapshot = scenario._directory(snapshot_dir, "Starting snapshot") if snapshot_dir is not None else _resolve(declared_path, source)
    _snapshot(snapshot, save_hash, expected)
    if any(scenario._overlap(out, other) for other in (source, snapshot, game)):
        raise RepeatError("Repeat output must be separate from the source run, snapshot, and game installation")
    if explicit_save_root is not None and scenario._overlap(out, explicit_save_root):
        raise RepeatError("Repeat output must be separate from the selected save root")
    backup = scenario._path(backup_dir) if backup_dir is not None else scenario._path(
        game / "dfhack-config" / "dfeval-backups" / uuid.uuid4().hex)
    if any(scenario._overlap(backup, other) for other in (source, out)):
        raise RepeatError("Save backup must be separate from source evidence and repeat output")
    installed = inspect_installation(game)
    found_game = installed["game"]["version"]
    found_dfhack = installed["dfhack"]["version"]
    if ((found_game is not None and not scenario.compatible_df_versions(found_game, expected["df_version"])) or
            (found_dfhack is not None and found_dfhack != expected["dfhack_version"])):
        raise RepeatError("Installed game or DFHack version differs from the source run")
    stop_original = config["stop_file"]
    stop_relocated = bool(stop_original and _resolve(stop_original, source) == source / "STOP")
    if stop_relocated:
        config["stop_file"] = None  # Runner resolves its standard STOP inside the new output directory.
    initial_raw = json_bytes(initial)
    plan = {"schema_version": 1, "kind": "native_control_repeat" if control is not None else "native_model_repeat",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": None if control is not None else replacement.model,
            "config": config, "policy_config": replacement.public_config(),
            "initial_expectation": expected,
            "source": {"manifest_sha256": _hash(manifest_raw), "events_sha256": _hash(events_raw),
                       "initial_sha256": _hash(initial_raw), "original_model": manifest["policy"]["model"],
                       "outcome": validated["outcome"], "warnings": validated["warnings"],
                       "stop_file_relocated": stop_relocated},
            "paths": {"game_dir": _relative(game, out), "snapshot_dir": _relative(snapshot, out),
                      "source_run": _relative(source, out), "run_dir": "run"},
            "restore_receipt": None}
    if explicit_save_root is not None:
        plan["paths"]["save_root"] = _relative(explicit_save_root, out)
    # Persist all source evidence before touching the save. An interrupted prepare
    # remains unsealed and cannot be run through load_repeat.
    out.mkdir(parents=True, exist_ok=False)
    (out / "source").mkdir()
    (out / "source" / "manifest.json").write_bytes(manifest_raw)
    (out / "source" / "initial.json").write_bytes(initial_raw)
    (out / "repeat.json").write_bytes(json_bytes(plan) + b"\n")
    receipt = scenario.restore_save(snapshot, game, save_name=expected["save_directory"],
                                    backup_dir=backup, game_stopped=game_stopped,
                                    expected_manifest_sha256=save_hash, save_root=explicit_save_root)
    for key in ("snapshot_directory", "save_directory", "backup_directory", "save_root"):
        if receipt.get(key) is not None:
            receipt[key] = _relative(Path(receipt[key]), out)
    plan["restore_receipt"] = receipt
    plan["seal_sha256"] = _seal(plan)
    temporary = out / "repeat.json.tmp"
    temporary.write_bytes(json_bytes(plan) + b"\n")
    temporary.replace(out / "repeat.json")
    return load_repeat(out)


def load_repeat(output_dir: str | Path) -> dict[str, Any]:
    """Verify the recipe, retained source evidence, and snapshot without game calls.

    Disk saves may change after the operator loads the game. Their initial native
    fingerprint must therefore be verified by run_experiment before its first
    policy call; this read-only loader never assumes the running game is restored.
    """
    root = scenario._directory(output_dir, "Repeat output")
    plan = _json(_read(root / "repeat.json", PLAN_LIMIT))
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS or plan.get("schema_version") != 1 or type(plan.get("schema_version")) is not int or plan.get("kind") not in ("native_model_repeat", "native_control_repeat"):
        raise RepeatError("Repeat recipe is malformed or preparation did not finish; it cannot authorize a run")
    if _digest(plan["seal_sha256"], "Recipe seal") != _seal(plan):
        raise RepeatError("Repeat recipe seal mismatch; settings or evidence references changed")
    path_fields = {"game_dir", "snapshot_dir", "source_run", "run_dir"}
    if (not isinstance(plan["source"], dict) or not isinstance(plan["paths"], dict) or
            set(plan["paths"]) not in (path_fields, path_fields | {"save_root"}) or plan["paths"]["run_dir"] != "run"):
        raise RepeatError("Repeat recipe has malformed source or paths")
    manifest_raw = _read(root / "source" / "manifest.json", PLAN_LIMIT)
    initial_raw = _read(root / "source" / "initial.json", INITIAL_LIMIT)
    for key, raw in (("manifest_sha256", manifest_raw), ("initial_sha256", initial_raw)):
        if plan["source"].get(key) != _hash(raw):
            raise RepeatError("Retained source evidence hash mismatch")
    _digest(plan["source"].get("events_sha256"), "Source event hash")
    manifest, initial = _json(manifest_raw), _json(initial_raw)
    if not isinstance(manifest, dict) or not isinstance(initial, dict):
        raise RepeatError("Retained source manifest and initial snapshot must be objects")
    expected = _expectation(manifest, initial)
    if expected != plan["initial_expectation"]:
        raise RepeatError("Recipe native starting expectation differs from source evidence")
    if plan["source"].get("original_model") != manifest.get("policy", {}).get("model"):
        raise RepeatError("Source model identity disagrees with retained manifest")
    original = _clone_policy(manifest["policy"])
    if plan["kind"] == "native_control_repeat":
        policy_from_plan(plan)  # Only an exact, identified idle/rule baseline is permitted.
    else:
        replacement = _clone_policy(original.public_config(), plan["model"])
        if replacement.model == original.model or replacement.public_config() != plan["policy_config"]:
            raise RepeatError("Repeat must change only the model identity, preserving all model connection settings")
    config = _config(manifest)
    if type(plan["source"].get("stop_file_relocated")) is not bool:
        raise RepeatError("Missing stop-file relocation metadata")
    if plan["source"]["stop_file_relocated"]:
        config["stop_file"] = None
    if config != plan["config"]:
        raise RepeatError("Repeat experiment settings differ from source evidence")
    game = scenario._directory(_resolve(plan["paths"]["game_dir"], root), "Game installation")
    if capture_environment(game)["fingerprint_sha256"] != expected["game_environment_sha256"]:
        raise RepeatError("Installation configuration differs from the source run; restore the original recorded settings before running this repeat")
    snapshot = _resolve(plan["paths"]["snapshot_dir"], root)
    _snapshot(snapshot, _digest(config["starting_save_sha256"], "Starting-save identity"), expected)
    receipt = plan["restore_receipt"]
    if not isinstance(receipt, dict) or receipt.get("kind") != "dfeval_local_restore_record" or receipt.get("integrity_verified") is not True or receipt.get("game_stopped_attested") is not True or receipt.get("snapshot_manifest_sha256") != config["starting_save_sha256"] or receipt.get("save_name") != expected["save_directory"]:
        raise RepeatError("Repeat has no matching verified save-restore receipt")
    if _resolve(receipt.get("snapshot_directory"), root) != snapshot:
        raise RepeatError("Restore receipt points to a different snapshot")
    restored = _resolve(receipt.get("save_directory"), root)
    explicit_save_root = _resolve(plan["paths"]["save_root"], root) if "save_root" in plan["paths"] else None
    save_roots = scenario._save_roots(game, explicit_save_root)
    if restored not in tuple(save_root / expected["save_directory"] for save_root in save_roots):
        raise RepeatError("Restore receipt is outside the selected save root(s)")
    if receipt.get("save_root") is not None and _resolve(receipt["save_root"], root) != restored.parent:
        raise RepeatError("Restore receipt save root disagrees with its restored save directory")
    if explicit_save_root is not None and receipt.get("save_root") is None:
        raise RepeatError("An explicit save-root recipe requires its matching restore receipt")
    run_dir = scenario._path(root / "run")
    if run_dir.parent != root:
        raise RepeatError("Repeat run output must stay inside the recipe directory")
    if run_dir.exists() and (not run_dir.is_dir() or any(run_dir.iterdir())):
        raise RepeatError("This recipe already has run evidence; prepare a new repeat output for another run")
    plan["resolved_paths"] = {"recipe_dir": str(root), "game_dir": str(game), "snapshot_dir": str(snapshot),
                              "source_run": str(_resolve(plan["paths"]["source_run"], root)),
                              "run_dir": str(run_dir), "restored_save_dir": str(restored),
                              "save_root": str(restored.parent)}
    return plan
