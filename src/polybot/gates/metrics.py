"""Metrics the gates are computed from. Pure functions, seeded where random.

Assumptions:
    * ``trades`` = SETTLED paper trades (gates use only settled PnL), one row per
      fill: market, wallet, stake (USDC spent), fee, net_pnl, resolved_ts, epoch.
    * ROI = sum(net_pnl) / sum(stake): stake-weighted, after fees and slippage.

Failure modes guarded against: point estimates without uncertainty, concentration
hiding behind an average, and drawdown or losing-streak arithmetic done ad hoc
in several places.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

TRADE_COLUMNS = ("market", "wallet", "stake", "fee", "net_pnl", "resolved_ts", "epoch")


@dataclass(frozen=True)
class Evidence:
    """Everything the gates look at. Built by ``polybot.backtest`` or the paper loop."""

    mode: str  # "live_forward" | "backtest" | "synthetic"
    start_ts: int
    end_ts: int
    trades: pd.DataFrame  # settled trades, TRADE_COLUMNS
    bankroll: float
    signals_reached_book: int
    signals_filled: int
    median_slippage: float | None
    pooled_oos_z: float | None
    frac_wallets_positive_oos: float | None
    epoch_pooled_z: tuple[float | None, ...] = ()
    stress_roi: float | None = None
    latency_roi: dict[int, float | None] = field(default_factory=dict)
    placebo_rois: tuple[float, ...] | None = None
    # When set, gate 4 compares THIS (the strategy scored by the same engine as the placebo
    # draws) instead of the ledger ROI, so both sides of the comparison are apples-to-apples.
    placebo_strategy_roi: float | None = None
    leakage_failures: int = 0
    recent_drift_errors: int = 0
    config_hash_start: str = ""
    config_hash_now: str = ""
    notes: tuple[str, ...] = ()


def roi(trades: pd.DataFrame) -> float | None:
    cost = float(trades["stake"].sum()) if len(trades) else 0.0
    return float(trades["net_pnl"].sum()) / cost if cost > 0 else None


def ordered(trades: pd.DataFrame) -> pd.DataFrame:
    return trades.sort_values(["resolved_ts", "market", "wallet"], kind="mergesort")


def max_drawdown(trades: pd.DataFrame, bankroll: float) -> float:
    """Largest peak-to-trough fall of settled equity, as a fraction of the bankroll."""
    if trades.empty:
        return 0.0
    eq = bankroll + np.cumsum(ordered(trades)["net_pnl"].to_numpy(dtype=np.float64))
    peak = np.maximum.accumulate(np.r_[bankroll, eq])[1:]
    return float(np.max(peak - eq) / bankroll) if eq.size else 0.0


def longest_losing_streak(wins: npt.NDArray[np.bool_]) -> int:
    best = cur = 0
    for w in wins:
        cur = 0 if w else cur + 1
        best = max(best, cur)
    return best


def streak_percentile(n: int, win_rate: float, pct: float, *, sims: int, seed: int) -> float:
    """``pct`` percentile of the longest losing streak in n Bernoulli(win_rate) trades."""
    if n == 0:
        return 0.0
    rng = np.random.default_rng(seed)
    w = rng.random((sims, n)) < win_rate
    # longest run of False per row, vectorised
    losses = (~w).astype(np.int32)
    run = np.zeros(sims, dtype=np.int32)
    best = np.zeros(sims, dtype=np.int32)
    for j in range(n):
        run = (run + 1) * losses[:, j]
        best = np.maximum(best, run)
    return float(np.percentile(best, pct))


def concentration(trades: pd.DataFrame, by: str) -> float:
    """Largest single group's PnL as a share of total PnL (1.0 if total <= 0)."""
    total = float(trades["net_pnl"].sum())
    if total <= 0 or trades.empty:
        return 1.0
    return float(trades.groupby(by)["net_pnl"].sum().max() / total)


def roi_without_top(trades: pd.DataFrame, k: int) -> float | None:
    if len(trades) <= k:
        return None
    keep = trades.sort_values("net_pnl", ascending=False, kind="mergesort").iloc[k:]
    return roi(keep)


def epoch_table(trades: pd.DataFrame, bankroll: float) -> list[dict[str, Any]]:
    out = []
    for ep, g in trades.groupby("epoch", sort=True):
        pnl = float(g["net_pnl"].sum())
        out.append(
            {
                "epoch": int(ep),  # type: ignore[arg-type]
                "n": len(g),
                "pnl": pnl,
                "roi": roi(g),
                "pnl_frac_bankroll": pnl / bankroll,
            }
        )
    return out


def rolling_roi_threshold(
    trades: pd.DataFrame, window: int, pct: float, *, sims: int, seed: int
) -> float | None:
    """``pct`` percentile of the ROI of ``window`` trades resampled from all trades."""
    if len(trades) < window:
        return None
    rng = np.random.default_rng(seed)
    pnl = trades["net_pnl"].to_numpy(dtype=np.float64)
    st = trades["stake"].to_numpy(dtype=np.float64)
    idx = rng.integers(0, len(trades), size=(sims, window))
    dist = pnl[idx].sum(axis=1) / st[idx].sum(axis=1)
    return float(np.percentile(dist, pct))
