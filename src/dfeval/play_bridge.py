"""Additional read-only game facts and the one existing bounded game action."""
from __future__ import annotations
from .live import LiveBridge

TOOLS = [
    {"name": "game_workshops", "arguments": {}, "description": "Read native workshops and their queued jobs."},
    {"name": "game_brew", "arguments": {"workshop_id": "integer", "quantity": "integer 1..10"}, "description": "Request ordinary plant-brewing jobs at a completed Still."},
]

class PlayBridge:
    def __init__(self, game_dir, *, timeout=30):
        self.game_dir = LiveBridge(game_dir, timeout=timeout).game_dir
        self.timeout = timeout
        self._bridge = None

    def install_script(self):
        # LiveBridge is the installed resident script; play uses its validated IPC.
        self._bridge = LiveBridge(self.game_dir, timeout=self.timeout)
        return self._bridge.install_script()

    def start_command(self):
        if self._bridge is None: self.install_script()
        return self._bridge.start_command()

    def dispatch(self, name, args):
        if self._bridge is None: raise RuntimeError("game bridge has not started")
        if name == "game_workshops":
            if args: raise ValueError("game_workshops takes no arguments")
            return {"workshops": self._bridge.observe().get("workshops", [])}
        if name == "game_brew":
            if set(args) != {"workshop_id", "quantity"}: raise ValueError("game_brew requires workshop_id and quantity")
            return self._bridge.queue_brew(args["workshop_id"], args["quantity"])
        raise ValueError(f"unknown game tool: {name}")
