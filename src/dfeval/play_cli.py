"""The beginner-facing launcher. Old research tools live behind 'legacy'."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import uuid

from .inference import TextClient
from .play import LearningLoop, OBJECTIVE, PlayLimits, SpendAccount


CONFIG = "play.local.toml"
DEFAULTS = {"provider": "ollama", "mode": "local", "budget": 10,
            "input_price": 0, "output_price": 0, "timeout": 120,
            "max_calls": 256, "output_tokens": 524288, "output_per_call": 4096,
            "context_bytes": 128000, "max_ticks": 1209600, "wall_seconds": 3600}
FIELDS = {*DEFAULTS, "model", "endpoint", "api_key_env", "df_path", "num_ctx", "token_limit_field"}


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Play settings must be a TOML file smaller than 64 KiB")
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(value) != {"play"} or not isinstance(value["play"], dict) or set(value["play"]) - FIELDS:
        raise ValueError("Settings must contain one [play] table with recognized play options")
    return value["play"]


def setup(path: Path) -> int:
    print("Dwarf Fortress learning setup. This saves settings and makes no model calls.")
    print("Install DF + matching DFHack, then load an ordinary new embark and pause it.")
    if path.exists():
        raise ValueError(f"Settings already exist at {path}; edit that file to change them")
    game = input("Dwarf Fortress folder [dwarfFortressItself]: ").strip().strip('"') or "dwarfFortressItself"
    mode = input("Model connection: local or cloud [local]: ").strip().lower() or "local"
    if mode not in ("local", "cloud"):
        raise ValueError("Choose local or cloud")
    values = {"df_path": str(Path(game).expanduser().resolve()), "mode": mode}
    if mode == "local":
        provider = input("Server: ollama or chat_completions [ollama]: ").strip() or "ollama"
        values["provider"] = provider
        if provider == "chat_completions":
            values["endpoint"] = input("Full local chat/completions URL: ").strip()
        elif provider != "ollama":
            raise ValueError("Choose ollama or chat_completions")
        else:
            values["num_ctx"] = 32768
    else:
        values.update(provider="chat_completions", budget=10)
        values["endpoint"] = input("Provider's full HTTPS chat/completions URL: ").strip()
        values["api_key_env"] = input("Environment variable holding your API key [DF_MODEL_KEY]: ").strip() or "DF_MODEL_KEY"
        print("Enter current USD prices per million tokens from your provider, including any long-context premium.")
        values["input_price"] = float(input("Input price per million tokens: ").strip())
        values["output_price"] = float(input("Output price per million tokens: ").strip())
        SpendAccount(10, values["input_price"], values["output_price"], cloud=True)
    values["model"] = input("Exact model name: ").strip()
    # Validate endpoint and option shapes without calling the model or storing a key.
    make_client({**DEFAULTS, **values})
    text = "# Local settings; this file contains no API key.\n[play]\n" + "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items())
    with path.open("x", encoding="utf-8") as dest:
        dest.write(text)
    print(f"Saved {path}. Load your fortress, then run: python start.py play")
    return 0


def make_client(values: dict) -> TextClient:
    return TextClient(provider=values["provider"], mode=values["mode"], model=values.get("model", ""),
                      endpoint=values.get("endpoint"), api_key_env=values.get("api_key_env"),
                      timeout=values["timeout"], max_output_tokens=values["output_per_call"],
                      max_request_bytes=min(16_000_000, values["context_bytes"] + 65536),
                      num_ctx=values.get("num_ctx"), token_limit_field=values.get("token_limit_field"))


def bootstrap(bridge) -> None:
    bridge.install_script()
    result = subprocess.run(bridge.start_command(), cwd=bridge.game_dir, shell=False,
                            capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("Could not connect to DFHack. Start DF with DFHack and load a fortress first.")


def play(args) -> int:
    values = {**DEFAULTS, **load_settings(Path(args.config))}
    for field in FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            values[field] = value
    if not values.get("model"):
        raise ValueError("Choose a model first: python start.py setup (or pass --model)")
    if values["mode"] == "cloud" and "provider" not in load_settings(Path(args.config)) and args.provider is None:
        values["provider"] = "chat_completions"
    limits = PlayLimits(**{key: values[key] for key in PlayLimits.__dataclass_fields__})
    account = SpendAccount(values["budget"], values["input_price"], values["output_price"], cloud=values["mode"] == "cloud")
    client = make_client(values)
    key_name = values.get("api_key_env")
    if client.mode == "cloud" and not os.environ.get(key_name or ""):
        raise ValueError(f"Set the {key_name} environment variable to your provider key before starting")
    game_dir = Path(values.get("df_path") or os.environ.get("DFEVAL_DF_PATH") or "dwarfFortressItself").expanduser().resolve()
    # Read-only file preflight comes before output creation, bridge installation or inference.
    from .live import LiveBridge, resolve_dfhack_runner
    resolve_dfhack_runner(game_dir)
    from .play_bridge import PlayBridge, TOOLS as GAME_TOOLS
    from .play_workspace import Workspace, TOOLS as WORKSPACE_TOOLS
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = Path(args.out or f"runs/play-{stamp}-{uuid.uuid4().hex[:6]}").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    # The model sees only files under workspace, never settings, evidence or credentials.
    workspace = Workspace(output / "workspace")
    native = LiveBridge(game_dir, timeout=30)
    extra = PlayBridge(game_dir, timeout=30)
    game = GameTools(native, extra)
    loop = None
    try:
        bootstrap(native)
        status = native.status()
        if any(status.get(key) is not True for key in ("world_loaded", "map_loaded", "fortress_mode")):
            raise ValueError("Load a fortress before starting. World generation and embark still need you.")
        native.pause()
        bootstrap(extra)
        print(f"Run: {output}\nObjective: {args.objective}\nWorkspace starts empty.", flush=True)
        print("Watch the game window. Ctrl+C stops the run; the runner then attempts to pause the game.", flush=True)
        loop = LearningLoop(client, game, workspace, output, limits=limits, account=account,
                            objective=args.objective, game_tools=GAME_TOOLS, workspace_tools=WORKSPACE_TOOLS)
        summary = loop.run()
        print(f"Stopped: {summary['stop_reason']}\nRead: {output / 'README.md'}")
        return 1 if summary["error"] or not summary["pause_confirmed"] else 0
    finally:
        if loop is None:
            try:
                native.pause()
            except Exception:
                pass


class GameTools:
    def __init__(self, native, extra):
        self.native, self.extra = native, extra

    def observe(self):
        raw = self.native.observe()
        # Native state, without the old brewing-task annotations or host/session metadata.
        return {key: value for key, value in raw.items() if key in {
            "absolute_tick", "year", "year_tick", "citizens", "known_former_citizens",
            "jobs", "stocks", "errors"}}

    def pause(self):
        return self.native.pause()

    def advance_ticks(self, ticks):
        return self.native.advance_ticks(ticks)

    def dispatch(self, name, arguments):
        return self.extra.dispatch(name, arguments)


def inspect_run(path: Path) -> int:
    if (path / "result.json").is_file():
        print(json.dumps(json.loads((path / "result.json").read_text(encoding="utf-8")), indent=2))
    else:
        print("No final result yet; the run is active or was interrupted.")
    from .play_workspace import Workspace
    workspace = Workspace(path / "workspace")
    print("\nModel-created files:")
    print(json.dumps(workspace.dispatch("workspace_list", {}), indent=2))
    print(f"\nFull record: {path / 'events.jsonl'}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "legacy":
        from .cli import main as legacy
        return legacy(argv[1:] or ["--help"])
    # Installation diagnosis and local model discovery remain useful standalone commands.
    if argv and argv[0] in ("doctor", "models"):
        from .cli import main as legacy
        return legacy(argv)
    parser = argparse.ArgumentParser(description="Give an LLM a fresh start in Dwarf Fortress. No supplied strategy or memories.")
    sub = parser.add_subparsers(dest="command")
    configure = sub.add_parser("setup", help="save your game and model settings; no paid calls")
    configure.add_argument("--config", default=CONFIG)
    run = sub.add_parser("play", help="learn in the loaded fortress with an empty writable workspace")
    run.add_argument("--config", default=CONFIG)
    run.add_argument("--df-path")
    run.add_argument("--model")
    run.add_argument("--mode", choices=("local", "cloud"))
    run.add_argument("--provider", choices=("ollama", "chat_completions"))
    run.add_argument("--endpoint")
    run.add_argument("--api-key-env")
    run.add_argument("--token-limit-field", choices=("max_tokens", "max_completion_tokens"))
    run.add_argument("--num-ctx", type=int)
    run.add_argument("--budget", type=float, help="USD spending guard; default 10")
    run.add_argument("--input-price", type=float, help="USD per million input tokens; required for cloud")
    run.add_argument("--output-price", type=float, help="USD per million output tokens; required for cloud")
    run.add_argument("--timeout", type=float)
    for name in PlayLimits.__dataclass_fields__:
        run.add_argument("--" + name.replace("_", "-"), type=int)
    run.add_argument("--objective", default=OBJECTIVE)
    run.add_argument("--out", help="new output folder; existing folders are refused")
    inspect = sub.add_parser("inspect", help="show a learning run's spending, result and model-created files")
    inspect.add_argument("run")
    sub.add_parser("doctor", help="check DF and DFHack installation files")
    sub.add_parser("models", help="list models on your local server")
    sub.add_parser("legacy", help="older brewing benchmarks, reports and mock simulator")
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            return setup(Path(args.config))
        if args.command == "play":
            return play(args)
        if args.command == "inspect":
            return inspect_run(Path(args.run).expanduser().resolve())
        parser.print_help()
        print("\nFirst time: python start.py setup\nThen:       python start.py play")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
