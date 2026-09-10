"""The model's small, persistent, path-confined notebook."""
from __future__ import annotations

import json
from pathlib import Path

MAX_FILE_BYTES = 32_768
MAX_TOTAL_BYTES = 1_048_576
MAX_ROUTINE_STEPS = 12
TOOLS = [
    {"name": "workspace_list", "arguments": {}, "description": "List files the model has created in its workspace."},
    {"name": "workspace_read", "arguments": {"path": "relative path"}, "description": "Read one of your workspace files."},
    {"name": "workspace_write", "arguments": {"path": "relative path", "content": "text"}, "description": "Create or replace a text file in your workspace."},
    {"name": "workspace_delete", "arguments": {"path": "relative path"}, "description": "Delete one of your workspace files."},
    {"name": "workspace_run", "arguments": {"path": "relative JSON routine path"}, "description": "Load a JSON list of at most twelve {tool, arguments} steps for the host to execute."},
]

class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")

    def _path(self, raw):
        if type(raw) is not str or not raw or len(raw) > 240:
            raise ValueError("path must be a nonempty relative path")
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.drive or any(part in ("", ".", "..") for part in candidate.parts):
            raise ValueError("path must stay inside workspace")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("path must stay inside workspace")
        return resolved

    def _files(self):
        return [p for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()]

    def dispatch(self, tool, arguments):
        if tool == "workspace_list":
            if arguments: raise ValueError("workspace_list takes no arguments")
            return {"files": [{"path": str(p.relative_to(self.root)).replace("\\", "/"), "bytes": p.stat().st_size}
                              for p in sorted(self._files())]}
        if tool in ("workspace_read", "workspace_delete"):
            if set(arguments) != {"path"}: raise ValueError(f"{tool} requires path")
            path = self._path(arguments["path"])
            if not path.is_file() or path.is_symlink(): raise ValueError("workspace file not found")
            if tool == "workspace_read":
                return path.read_text(encoding="utf-8")
            path.unlink()
            return {"deleted": str(path.relative_to(self.root)).replace("\\", "/")}
        if tool == "workspace_write":
            if set(arguments) != {"path", "content"} or type(arguments["content"]) is not str:
                raise ValueError("workspace_write requires path and text content")
            path = self._path(arguments["path"])
            encoded = arguments["content"].encode("utf-8")
            if len(encoded) > MAX_FILE_BYTES: raise ValueError("workspace file exceeds 32 KiB")
            path.parent.mkdir(parents=True, exist_ok=True)
            total = sum(p.stat().st_size for p in self._files() if p != path)
            if total + len(encoded) > MAX_TOTAL_BYTES: raise ValueError("workspace total exceeds 1 MiB")
            path.write_bytes(encoded)
            return {"written": str(path.relative_to(self.root)).replace("\\", "/"), "bytes": len(encoded)}
        if tool == "workspace_run":
            if set(arguments) != {"path"}: raise ValueError("workspace_run requires path")
            value = json.loads(self.dispatch("workspace_read", arguments))
            if type(value) is not list or not 1 <= len(value) <= MAX_ROUTINE_STEPS:
                raise ValueError("routine must be a JSON list of 1..12 steps")
            result = []
            for step in value:
                if type(step) is not dict or set(step) != {"tool", "arguments"} or type(step["tool"]) is not str or type(step["arguments"]) is not dict:
                    raise ValueError("each routine step must contain tool and arguments")
                result.append(step)
            return result
        raise ValueError(f"unknown workspace tool: {tool}")
