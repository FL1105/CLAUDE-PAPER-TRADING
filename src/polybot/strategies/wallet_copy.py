"""Strategy A: copy new BUYs by a frozen, statistically selected wallet set.

Assumptions:
    * At each epoch start the wallet set is selected with the leakage-safe
      selector (BH on the trailing window) and FROZEN for the epoch.
    * On a BUY by a frozen wallet whose entry price is inside the eligible band
      [min_entry_price, max_entry_price], emit an entry Signal. The chase rule,
      sizing and fill happen in the PaperExecutor.
    * SELLs by frozen wallets are emitted as informational signals (hold to
      resolution in v1).

Failure mode guarded against: copying trades the selection never validated, such
as penny-picking entries outside the band, or wallets selected with future data.
"""

from __future__ import annotations

from polybot.config import Config
from polybot.execution.paper import PaperState, Signal
from polybot.selection.walkforward import DataView, Epoch, select_wallets
from polybot.strategies.base import MarketEvent


class WalletCopy:
    name = "wallet_copy"

    def __init__(self, cfg: Config, seed: int) -> None:
        self.cfg = cfg
        self.seed = seed
        self.frozen: frozenset[str] = frozenset()
        self.epoch: Epoch | None = None
        self.last_diagnostics: dict[str, object] = {}

    def on_epoch_start(self, epoch: Epoch, history: DataView) -> None:
        sel = select_wallets(history, epoch, self.cfg, self.seed + epoch.index)
        self.frozen = frozenset(sel.wallets)
        self.epoch = epoch
        self.last_diagnostics = sel.diagnostics

    def set_frozen(self, wallets: frozenset[str], epoch: Epoch) -> None:
        """Install a precomputed selection (e.g. loaded from disk by the forward loop)."""
        self.frozen = wallets
        self.epoch = epoch

    def on_event(self, event: MarketEvent, state: PaperState) -> list[Signal]:
        if event.wallet not in self.frozen:
            return []
        if self.epoch is not None and not (self.epoch.start_ts <= event.ts < self.epoch.end_ts):
            return []  # selection is only valid inside its own epoch
        f = self.cfg.filters
        is_exit = event.side == "SELL"
        if not is_exit and not (f.min_entry_price <= event.price <= f.max_entry_price):
            return []
        return [
            Signal(
                strategy=self.name,
                condition_id=event.condition_id,
                token_id=event.token_id,
                ts=event.ts,
                wallet=event.wallet,
                wallet_price=event.price,
                wallet_trade_key=event.trade_key,
                category=event.category,
                kind="info_exit" if is_exit else "entry",
            )
        ]
