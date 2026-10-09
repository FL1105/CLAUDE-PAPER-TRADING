"""Kill switch: file flag that persists across restarts and is checked before EVERY order.

Engaged by: CLI (`python -m bot.cli kill`), dashboard button, or the drawdown HALT.
Released only by an explicit operator action (`release`)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from bot.config import ROOT

DEFAULT_PATH = ROOT / "state" / "KILL"


class KillSwitchEngaged(RuntimeError):
    pass


class KillSwitch:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else DEFAULT_PATH

    def engage(self, reason: str, by: str = "system") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"reason": reason, "by": by,
                                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")

    def release(self, by: str) -> None:
        if not by:
            raise ValueError("release requires an operator name")
        if self.path.exists():
            self.path.unlink()

    def is_engaged(self) -> bool:
        return self.path.exists()

    def info(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"reason": "unreadable flag file (treated as engaged)", "by": "system"}

    def assert_clear(self) -> None:
        if self.is_engaged():
            raise KillSwitchEngaged(str(self.info()))
