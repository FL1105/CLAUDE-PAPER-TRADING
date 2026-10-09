"""Real-money adapter placeholder. Intentionally unimplemented: see docs/RUNBOOK.md
(go-live checklist). Any attempt to use it raises NotEnabled."""
from __future__ import annotations

from bot.broker.base import NotEnabled


class LiveBroker:
    mode = "live"

    def __init__(self, *_, **__):
        raise NotEnabled("Live trading is not implemented or enabled. Operator decision required.")
