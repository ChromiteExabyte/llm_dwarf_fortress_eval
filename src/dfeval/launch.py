"""Portable local-model setup and policy construction; no game or server launch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tomllib
from typing import Any
from urllib.parse import urlsplit

from .policies import ChatCompletionsPolicy


DEFAULT_CONFIG = "dfeval.local.toml"
PROVIDERS = ("ollama", "lmstudio", "llamacpp", "compatible")
BASE_URLS = {"ollama": "http://127.0.0.1:11434", "lmstudio": "http://127.0.0.1:1234",
             "llamacpp": "http://127.0.0.1:8080"}
DEFAULTS = {"provider": "ollama", "context_size": 16384, "response_tokens": 512,
            "timeout": 300, "keep_alive": "10m", "seed": 0, "temperature": 0.0,
            "think": False, "response_format": "json_schema"}
FIELDS = frozenset({*DEFAULTS, "base_url", "model", "df_path", "simulation_fps"})
OLLAMA_OPTIONS = frozenset({"context_size", "keep_alive", "seed", "temperature", "think"})


def load_config(path: str | Path, *, required: bool = False) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        if required:
            raise ValueError(f"Configuration not found: {path}; run dfeval setup first")
        return {}
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Configuration must be a regular TOML file of at most 64 KiB")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid local TOML configuration") from exc
    if set(data) != {"benchmark"} or not isinstance(data["benchmark"], dict):
        raise ValueError("Configuration must contain one [benchmark] table")
    values = data["benchmark"]
    if set(values) - FIELDS:
        raise ValueError("Unknown configuration fields: " + ", ".join(sorted(set(values) - FIELDS)))
    result = dict(values)
    if "df_path" in result:
        if not isinstance(result["df_path"], str) or not result["df_path"]:
            raise ValueError("df_path must be a nonempty path string")
        local = Path(result["df_path"]).expanduser()
        result["df_path"] = str((path.parent / local).resolve() if not local.is_absolute() else local.resolve())
    return result


def settings(args: Any) -> dict[str, Any]:
    """Only explicit command-line values override the local configuration."""
    config_path = getattr(args, "config", None)
    configured = load_config(config_path or DEFAULT_CONFIG, required=config_path is not None)
    result = {**DEFAULTS, **configured}
    previous_provider = result["provider"]
    explicit = set(configured)
    for name in FIELDS:
        value = getattr(args, name, None)
        if value is not None:
            result[name] = value
            explicit.add(name)
    provider = result["provider"]
    if provider not in PROVIDERS:
        raise ValueError("provider must be ollama, lmstudio, llamacpp, or compatible")
    # Switching providers must not carry a different server's configured URL.
    if provider != previous_provider and getattr(args, "base_url", None) is None:
        result.pop("base_url", None)
        # Provider-specific settings from a different preset do not transfer.
        for key in OLLAMA_OPTIONS:
            if getattr(args, key, None) is None:
                result[key] = DEFAULTS[key]
                explicit.discard(key)
    result.setdefault("base_url", BASE_URLS.get(provider))
    if not result.get("base_url"):
        raise ValueError("--base-url is required for a compatible server")
    if not result.get("df_path"):
        result["df_path"] = os.environ.get("DFEVAL_DF_PATH") or next(
            (str(Path(p).resolve()) for p in ("dwarfFortressItself", "game") if Path(p).is_dir()), None)
    result["_explicit"] = explicit
    return result


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    return {key: value for key, value in {
        "name": row.get("name"), "digest": row.get("digest"),
        "parameter_size": details.get("parameter_size"),
        "quantization_level": details.get("quantization_level"),
    }.items() if isinstance(value, str)}


def make_policy(values: dict[str, Any], *, identity: dict[str, Any] | None = None):
    from .local_models import OllamaPolicy
    provider = values.get("provider", "ollama")
    if provider not in PROVIDERS:
        raise ValueError("Unknown local provider")
    explicit = values.get("_explicit", {key for key in OLLAMA_OPTIONS if key in values and values[key] != DEFAULTS[key]})
    if provider != "ollama" and set(explicit) & OLLAMA_OPTIONS:
        raise ValueError("Context size, keep-alive, seed, temperature and thinking flags are Ollama-specific; configure this server directly")
    if provider == "ollama" and "response_format" in explicit and values.get("response_format") != "json_schema":
        raise ValueError("Native Ollama uses the decision JSON schema; --response-format applies to compatible servers")
    model = values.get("model")
    if not model:
        raise ValueError("Choose a model with dfeval setup or --model; list installed models with dfeval models")
    base = values.get("base_url") or BASE_URLS.get(provider)
    if not isinstance(base, str):
        raise ValueError("A server --base-url is required")
    if provider == "ollama":
        return OllamaPolicy(model=model, base_url=base, model_identity=identity,
            max_completion_tokens=values["response_tokens"], timeout=values["timeout"],
            num_ctx=values["context_size"], temperature=values["temperature"], seed=values["seed"],
            keep_alive=values["keep_alive"], think=values["think"])
    # For compatible servers, base URLs name the server root or its /v1 prefix.
    parsed = urlsplit(base)
    if parsed.path.rstrip("/") not in ("", "/v1"):
        raise ValueError("Compatible --base-url must be the server root or end in /v1")
    endpoint = base.rstrip("/") + ("/chat/completions" if parsed.path.rstrip("/") == "/v1" else "/v1/chat/completions")
    return ChatCompletionsPolicy(mode="local", model=model, endpoint=endpoint,
        max_completion_tokens=values["response_tokens"], timeout=values["timeout"],
        response_format=values["response_format"])


def selected_model(values: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Read server metadata once before the measured run; never generate tokens."""
    from .local_models import discover_models
    # Validate every chosen request option before making even a metadata request.
    make_policy(values)
    report = discover_models(values["provider"], values["base_url"], timeout=5)
    matches = [row for row in report["models"] if row.get("name") == values["model"] or row.get("id") == values["model"]]
    if not matches:
        raise ValueError("Selected model is not listed by this server. Run dfeval models and use its exact name.")
    return make_policy(values, identity=_identity(matches[0])), report


def write_config(path: str | Path, values: dict[str, Any]) -> Path:
    path = Path(path).expanduser().resolve()
    if path.exists():
        raise ValueError(f"Configuration already exists; edit it or choose --out: {path}")
    make_policy(values)
    saved = {key: value for key, value in values.items() if key in FIELDS and value is not None}
    if values.get("provider", "ollama") != "ollama":
        for key in OLLAMA_OPTIONS:
            saved.pop(key, None)
    if saved.get("df_path"):
        game = Path(saved["df_path"]).expanduser().resolve()
        try:
            saved["df_path"] = Path(os.path.relpath(game, path.parent)).as_posix()
        except ValueError:  # Windows volumes can have no relative path.
            saved["df_path"] = game.as_posix()
    lines = ["# Local paths and model selection; no credentials. Kept out of Git.", "[benchmark]"]
    for key in sorted(saved):
        value = saved[key]
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Invalid setting: {key}")
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=True, allow_nan=False)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
    return path


def setup(args: Any, *, input_fn=input, output_fn=print, interactive: bool | None = None) -> Path:
    from .local_models import discover_models
    interactive = sys.stdin.isatty() if interactive is None else interactive
    provider = getattr(args, "provider", None)
    if provider is None and interactive:
        output_fn("Local model server: 1. Ollama  2. LM Studio  3. llama.cpp  4. Other compatible server")
        answer = input_fn("Choose [1]: ").strip() or "1"
        if answer not in ("1", "2", "3", "4"):
            raise ValueError("Choose a number from 1 to 4")
        provider = PROVIDERS[int(answer) - 1]
    provider = provider or "ollama"
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    base = getattr(args, "base_url", None) or BASE_URLS.get(provider)
    if not base and interactive:
        base = input_fn("Server base URL: ").strip()
    discovered = discover_models(provider, base, timeout=5)
    rows = [r for r in discovered["models"] if r.get("cloud_backed") is not True]
    if not rows:
        raise ValueError("No local models are installed/served. Download a model in your server, then run setup again.")
    model = getattr(args, "model", None)
    if not model:
        if len(rows) == 1:
            model = rows[0]["name"]
            output_fn("Using the server's only local model: " + model)
        elif interactive:
            for index, row in enumerate(rows, 1):
                output_fn(f"{index}. {row['name']}")
            answer = input_fn("Choose model number: ").strip()
            if not answer.isascii() or not answer.isdecimal() or not 1 <= int(answer) <= len(rows):
                raise ValueError("Choose one of the listed model numbers")
            model = rows[int(answer) - 1]["name"]
        else:
            raise ValueError("Several models are available; select --model explicitly")
    if not any(row.get("name") == model or row.get("id") == model for row in rows):
        raise ValueError("The chosen model is not listed as a local model on this server")
    game = getattr(args, "df_path", None) or os.environ.get("DFEVAL_DF_PATH") or next(
        (p for p in ("dwarfFortressItself", "game") if Path(p).is_dir()), None)
    if not game and interactive:
        game = input_fn("Dwarf Fortress + DFHack folder: ").strip()
    if not game or not Path(game).expanduser().is_dir():
        raise ValueError("Provide the installed game folder with --df-path")
    values = {**DEFAULTS, "provider": provider, "base_url": base, "model": model,
              "df_path": str(Path(game).expanduser().resolve())}
    path = write_config(getattr(args, "out", None) or DEFAULT_CONFIG, values)
    output_fn(f"Saved {path}. Load your prepared fortress, then run dfeval benchmark.")
    return path
