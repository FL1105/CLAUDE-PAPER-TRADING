"""PaperExecutor: risk limits, chase rule, book-walk fills, fees, settlement, ledger.

Assumptions:
    * Virtual bankroll ``paper.bankroll_usdc``. Each signal stakes a FIXED
      ``stake_frac`` of the starting bankroll: no compounding, no Kelly, no
      martingale (DECISIONS D4).
    * Limits: at most ``max_market_frac`` of the bankroll per market,
      ``max_total_exposure_frac`` open in total, ``max_concurrent_positions``
      positions, and never more than the available cash. No leverage, no shorting.
    * Hold to resolution: wallet exits are logged as informational signals only.
    * The book passed in is the fill reference: a real snapshot in live_forward
      mode, a synthetic or tape-derived book in backtests (evidence_mode says which).

Failure modes guarded against:
    * Filling what could not be filled: the chase rule skips a signal if visible
      depth within ``max_chase`` of the best ask cannot absorb the stake, or if
      the price is beyond wallet_price + max_chase. Every skip is written with
      its reason.
    * Lost or double-counted state after a crash: the state is rebuilt from the
      append-only ledger on start-up; a signal and its fill are written in one
      transaction; duplicate signals (same strategy + wallet trade) are ignored.
    * Mid-test tuning: every row carries the config hash.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from polybot.config import Config
from polybot.data.schemas import BookSnapshot
from polybot.data.store import Store
from polybot.execution.book import FillResult, depth_within, walk_book
from polybot.execution.fees import fee_coeff, fill_fee

EvidenceMode = Literal["live_forward", "backtest", "synthetic"]
SignalKind = Literal["entry", "info_exit"]


@dataclass(frozen=True)
class Signal:
    strategy: str
    condition_id: str
    token_id: str
    ts: int
    wallet: str | None = None
    wallet_price: float | None = None
    wallet_trade_key: str | None = None
    category: str | None = None
    kind: SignalKind = "entry"


@dataclass(frozen=True)
class Position:
    fill_id: int
    condition_id: str
    token_id: str
    wallet: str | None
    cost: float
    fee: float
    shares: float
    created_ts: int


@dataclass
class PaperState:
    bankroll0: float
    cash: float
    realized_pnl: float = 0.0
    open: dict[int, Position] = field(default_factory=dict)

    @property
    def exposure(self) -> float:
        return sum(p.cost for p in self.open.values())

    def market_exposure(self, condition_id: str) -> float:
        return sum(p.cost for p in self.open.values() if p.condition_id == condition_id)

    @property
    def settled_equity(self) -> float:
        return self.bankroll0 + self.realized_pnl


@dataclass(frozen=True)
class Outcome:
    status: Literal["filled", "skipped", "info", "duplicate"]
    reason: str = ""
    signal_id: int | None = None
    fill_id: int | None = None
    fill: FillResult | None = None
    fee: float = 0.0


class PaperExecutor:
    def __init__(
        self,
        store: Store,
        cfg: Config,
        *,
        evidence_mode: EvidenceMode,
        config_hash: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.mode = evidence_mode
        self.config_hash = config_hash
        self.clock = clock
        self.state = self.load_state()

    # --- state ------------------------------------------------------------------
    def load_state(self) -> PaperState:
        """Rebuild state from the ledger (crash recovery: the DB is the truth)."""
        b0 = self.cfg.paper.bankroll_usdc
        fills = self.store.fills_frame()
        sets = self.store.settlements_frame()
        st = PaperState(bankroll0=b0, cash=b0)
        if len(fills):
            st.cash -= float((fills["stake_usdc"] + fills["fee"]).sum())
        settled_ids = set(sets["fill_id"].astype(int)) if len(sets) else set()
        if len(sets):
            st.cash += float(sets["payout"].sum())
            st.realized_pnl = float(sets["net_pnl"].sum())
        rows: list[dict[Any, Any]] = fills.to_dict("records")
        for r in rows:
            fid = int(r["fill_id"])
            if fid not in settled_ids:
                w = r["wallet"]
                st.open[fid] = Position(
                    fid,
                    str(r["condition_id"]),
                    str(r["token_id"]),
                    None if w is None else str(w),
                    float(r["stake_usdc"]),
                    float(r["fee"]),
                    float(r["shares"]),
                    int(r["created_ts"]),
                )
        return st

    # --- signals -------------------------------------------------------------------
    def _signal_row(self, s: Signal, status: str, reason: str | None) -> dict[str, Any]:
        return {
            "created_ts": int(self.clock()),
            "strategy": s.strategy,
            "wallet": s.wallet,
            "condition_id": s.condition_id,
            "token_id": s.token_id,
            "wallet_trade_key": s.wallet_trade_key,
            "wallet_price": s.wallet_price,
            "status": status,
            "skip_reason": reason,
            "evidence_mode": self.mode,
            "config_hash": self.config_hash,
        }

    def _skip(self, s: Signal, reason: str) -> Outcome:
        sid, _ = self.store.record_signal_and_fill(self._signal_row(s, "skipped", reason), None)
        return Outcome("skipped", reason, sid)

    def _risk_check(self, s: Signal, stake: float) -> str | None:
        pc, b0, st = self.cfg.paper, self.state.bankroll0, self.state
        if len(st.open) >= pc.max_concurrent_positions:
            return "risk:max_concurrent_positions"
        if st.market_exposure(s.condition_id) + stake > pc.max_market_frac * b0 + 1e-9:
            return "risk:max_per_market"
        if st.exposure + stake > pc.max_total_exposure_frac * b0 + 1e-9:
            return "risk:max_total_exposure"
        if st.cash < stake * 1.1:  # stake plus a fee buffer
            return "risk:insufficient_cash"
        return None

    def handle(
        self,
        s: Signal,
        book: BookSnapshot | None,
        shadow_books: Mapping[int, BookSnapshot | None] | None = None,
        *,
        book_snapshot_id: int | None = None,
        fee_multiplier: float = 1.0,
    ) -> Outcome:
        if self.store.signal_exists(s.strategy, s.wallet_trade_key):
            return Outcome("duplicate", "already_processed")
        halted = self.store.get_halt()
        if halted is not None and s.kind == "entry":
            return self._skip(s, f"halted:{halted}")  # kill switch: no new risk
        if s.kind == "info_exit":
            sid, _ = self.store.record_signal_and_fill(
                self._signal_row(s, "info", "wallet_exit_not_mirrored"), None
            )
            return Outcome("info", "wallet_exit_not_mirrored", sid)

        ex, pc = self.cfg.execution, self.cfg.paper
        stake = pc.stake_frac * self.state.bankroll0
        risk = self._risk_check(s, stake)
        if risk:
            return self._skip(s, risk)
        if book is None or not book.asks:
            return self._skip(s, "empty_book")

        best = book.asks[0].price
        max_price = None if s.wallet_price is None else s.wallet_price + ex.max_chase
        if max_price is not None and best > max_price + 1e-9:
            return self._skip(s, "chase:best_ask_beyond_max_chase")
        if depth_within(book.asks, best, ex.max_chase) < stake - 1e-9:
            return self._skip(s, "chase:insufficient_depth_within_max_chase")
        tick = book.tick_size or ex.default_tick_size
        fr = walk_book(
            book.asks,
            stake,
            tick=tick,
            insufficient=ex.insufficient_depth,
            max_price=max_price,
            share_decimals=ex.share_decimals,
        )
        if not fr.filled or fr.avg_price is None or fr.best_ask is None:
            return self._skip(s, f"fill:{fr.reason}")
        coeff = fee_coeff(self.cfg.fees, s.category, fee_multiplier)
        fee = fill_fee(fr.levels, coeff)

        shadows: dict[str, Any] = {}
        for m in ex.shadow_multipliers:
            sb = (shadow_books or {}).get(m)
            if sb is None or not sb.asks:
                shadows[str(m)] = {"status": "missing"}
                continue
            sr = walk_book(
                sb.asks,
                stake,
                tick=sb.tick_size or tick,
                insufficient=ex.insufficient_depth,
                share_decimals=ex.share_decimals,
            )
            shadows[str(m)] = {
                "status": sr.status,
                "reason": sr.reason,
                "avg_price": sr.avg_price,
                "shares": sr.shares,
                "cost": sr.cost,
                "fee": fill_fee(sr.levels, coeff) if sr.filled else 0.0,
                "book_ts": sb.ts,
            }

        fill_row = {
            "created_ts": int(self.clock()),
            "wallet": s.wallet,
            "condition_id": s.condition_id,
            "token_id": s.token_id,
            "wallet_price": s.wallet_price,
            "fill_price": fr.avg_price,
            "best_ask": fr.best_ask,
            "slippage": fr.slippage_vs(s.wallet_price),
            "slippage_vs_best": fr.avg_price - fr.best_ask,
            "fee": fee,
            "fee_coeff": coeff,
            "intended_stake": stake,
            "stake_usdc": fr.cost,
            "shares": fr.shares,
            "shadow_fills_json": json.dumps(shadows, sort_keys=True),
            "book_snapshot_id": book_snapshot_id,
            "evidence_mode": self.mode,
            "config_hash": self.config_hash,
        }
        sid, fid = self.store.record_signal_and_fill(self._signal_row(s, "filled", None), fill_row)
        assert fid is not None
        self.state.cash -= fr.cost + fee
        self.state.open[fid] = Position(
            fid, s.condition_id, s.token_id, s.wallet, fr.cost, fee, fr.shares, int(self.clock())
        )
        return Outcome("filled", "", sid, fid, fr, fee)

    # --- settlement ------------------------------------------------------------------
    def settle_market(
        self,
        condition_id: str,
        payout_per_share: Mapping[str, float],
        resolution_status: str,
        resolved_ts: int | None,
    ) -> list[int]:
        """Settle every open position in a market. ``payout_per_share`` maps token -> payout
        (1/0 when resolved; the refund value, e.g. 0.5, when void). Unknown tokens raise."""
        if resolution_status not in ("resolved", "void"):
            raise ValueError(f"cannot settle a market with status {resolution_status!r}")
        out = []
        for fid, pos in sorted(self.state.open.items()):
            if pos.condition_id != condition_id:
                continue
            if pos.token_id not in payout_per_share:
                raise KeyError(f"no payout for token {pos.token_id} in {condition_id}")
            px = float(payout_per_share[pos.token_id])
            if not 0.0 <= px <= 1.0:
                raise ValueError("payout per share must be in [0, 1]")
            payout = pos.shares * px
            net = payout - pos.cost - pos.fee
            sid = self.store.record_settlement(
                {
                    "fill_id": fid,
                    "created_ts": int(self.clock()),
                    "resolution_status": resolution_status,
                    "resolved_ts": resolved_ts,
                    "payout": payout,
                    "net_pnl": net,
                    "supersedes": None,
                    "config_hash": self.config_hash,
                }
            )
            self.state.cash += payout
            self.state.realized_pnl += net
            del self.state.open[fid]
            out.append(sid)
        return out


def payouts_for_market(
    resolution_status: str,
    winning_outcome_index: int | None,
    settlement_prices: list[float],
    tokens: Mapping[str, int],
) -> dict[str, float]:
    """token -> payout per share. Resolved: 1 for the winner, 0 otherwise. Void: the
    settlement (refund) price of each outcome. Never inferred from trade prices."""
    if resolution_status == "resolved":
        if winning_outcome_index is None:
            raise ValueError("resolved market without a winner")
        return {t: 1.0 if i == winning_outcome_index else 0.0 for t, i in tokens.items()}
    if resolution_status == "void":
        return {t: float(settlement_prices[i]) for t, i in tokens.items()}
    raise ValueError(f"market not settled: {resolution_status}")
