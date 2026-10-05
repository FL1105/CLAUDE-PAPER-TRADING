"""Strategy interface: the same ledger, gates and report judge every idea.

Assumptions:
    * A strategy sees the world only through ``on_epoch_start`` (a leakage-proof
      ``DataView`` cut at the epoch start) and ``on_event`` (one market event at a
      time, in time order). It returns Signals; the PaperExecutor decides fills.
    * Gates, ledger and report do not know which strategy produced a trade.

Failure mode guarded against: strategy-specific evaluation shortcuts. Every
strategy is forced through the same execution and gate path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from polybot.execution.paper import PaperState, Signal
from polybot.selection.walkforward import DataView, Epoch


@dataclass(frozen=True)
class MarketEvent:
    """A trade observed on the public feed."""

    ts: int
    wallet: str
    condition_id: str
    token_id: str
    side: str
    price: float
    size_shares: float
    trade_key: str
    category: str | None = None


class Strategy(Protocol):
    name: str

    def on_epoch_start(self, epoch: Epoch, history: DataView) -> None: ...

    def on_event(self, event: MarketEvent, state: PaperState) -> list[Signal]: ...
