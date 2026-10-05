"""Wallet eligibility / anti-fake-alpha filters (all thresholds in ``filters`` config).

Assumptions:
    * Inputs come from ONE selection window (already leakage-safe; see walkforward).
    * ``trades`` has wallet, market, outcome, side, price, shares, ts.
    * ``resolved_ts`` maps market -> resolution time for the markets in the window.

Each filter logs how many wallets (or entries) it removed. Order:
    1. likely market maker: share of two-sided markets above max
    2. entry price band (removes ENTRIES priced outside [min, max]: penny-picking
       at 0.97 is not copyable edge, and 0.02 longshots are lottery tickets)
    3. hyperactive: trades per active day above max
    4. not hold-to-resolution: share of positions sold before resolution above max
       (we cannot evaluate hold-to-resolution edge for them)
    5. too few resolved markets
    6. too short active span
    7. one market contributes more than max share of positive PnL
    Wash / self-trading patterns are FLAGGED (reported), never silently dropped.

Failure modes guarded against: market makers, arbitrage bots, penny-pickers and
one-hit wonders looking "skilled" by win rate or PnL. Every removal is counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from polybot.config import FiltersConfig
from polybot.stats.edge import Units, stake_cap, wallet_edges

DAY = 86_400
WASH_WINDOW_S = 60
WASH_PRICE_TOL = 0.005
WASH_MIN_EVENTS = 3


@dataclass(frozen=True)
class FilterStep:
    name: str
    removed: int
    remaining: int
    unit: str = "wallets"


@dataclass
class EligibilityResult:
    eligible: list[str]
    units: pd.DataFrame  # units after the price-band filter
    edges: pd.DataFrame  # edges for ALL wallets with units (pre wallet filters)
    cap: float
    steps: list[FilterStep] = field(default_factory=list)
    flags: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=["wallet", "flag", "detail"])
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible": len(self.eligible),
            "steps": [s.__dict__ for s in self.steps],
            "flags": len(self.flags),
        }


def wash_flags(trades: pd.DataFrame) -> pd.DataFrame:
    """Flag wallets that buy and sell the same token within seconds at ~the same price."""
    if trades.empty or "SELL" not in set(trades["side"]):
        return pd.DataFrame(columns=["wallet", "flag", "detail"])
    t = trades.sort_values(["wallet", "market", "outcome", "ts"])
    same = (
        (t["wallet"].to_numpy()[1:] == t["wallet"].to_numpy()[:-1])
        & (t["market"].to_numpy()[1:] == t["market"].to_numpy()[:-1])
        & (t["outcome"].to_numpy()[1:] == t["outcome"].to_numpy()[:-1])
        & (t["side"].to_numpy()[1:] != t["side"].to_numpy()[:-1])
        & (np.diff(t["ts"].to_numpy()) <= WASH_WINDOW_S)
        & (np.abs(np.diff(t["price"].to_numpy())) <= WASH_PRICE_TOL)
    )
    wallets = t["wallet"].to_numpy()[1:][same]
    counts = pd.Series(wallets).value_counts()
    hit = counts[counts >= WASH_MIN_EVENTS].sort_index()
    return pd.DataFrame(
        {
            "wallet": hit.index.astype(str),
            "flag": "possible_wash_trading",
            "detail": [f"{n} buy/sell round trips within {WASH_WINDOW_S}s" for n in hit.to_numpy()],
        }
    )


def apply_filters(
    trades: pd.DataFrame,
    units: Units,
    resolved_ts: pd.Series,
    cfg: FiltersConfig,
    *,
    cap_quantile: float,
    small_n_exact_threshold: int,
    seed: int,
    n_mc: int = 2000,
) -> EligibilityResult:
    steps: list[FilterStep] = []
    u = units.units
    two = units.two_sided
    alive = set(u["wallet"].unique()) | set(two["wallet"].unique())

    def step(name: str, drop: set[str]) -> None:
        nonlocal alive
        removed = alive & drop
        alive -= removed
        steps.append(FilterStep(name, len(removed), len(alive)))

    # 1. market makers: share of two-sided markets (counted before anything else so
    #    wallets that ONLY trade both sides are attributed here)
    one_sided = u.groupby("wallet").size()
    two_n = two.groupby("wallet").size() if len(two) else pd.Series(dtype=float)
    idx = one_sided.index.union(two_n.index)
    o, t2 = one_sided.reindex(idx, fill_value=0), two_n.reindex(idx, fill_value=0)
    ratio = t2 / (o + t2)
    step("market_maker_two_sided", set(ratio.index[ratio > cfg.max_two_sided_ratio]))

    # 2. price band (entry level)
    band = u["p_bar"].between(cfg.min_entry_price, cfg.max_entry_price)
    steps.append(FilterStep("entry_price_band", int((~band).sum()), int(band.sum()), "entries"))
    u = u.loc[band].reset_index(drop=True)
    step("no_entries_left_after_price_band", alive - set(u["wallet"].unique()))

    cap = stake_cap(u, cap_quantile)
    edges = wallet_edges(
        u, cap, small_n_exact_threshold=small_n_exact_threshold, seed=seed, n_mc=n_mc
    )
    e = edges.set_index("wallet")

    # 3. hyperactive
    g = trades.groupby("wallet")["ts"]
    span_days = ((g.max() - g.min()) / DAY).clip(lower=1.0)
    per_day = g.size() / span_days
    step("trades_per_day", set(per_day.index[per_day > cfg.max_trades_per_day]))

    # 4. positions closed before resolution
    sells = trades.loc[trades["side"] == "SELL", ["wallet", "market", "outcome", "ts"]]
    if len(sells):
        s = sells.assign(res_ts=sells["market"].map(resolved_ts))
        s = s.loc[s["ts"] < s["res_ts"]].drop_duplicates(["wallet", "market", "outcome"])
        closed = s.merge(
            units.units[["wallet", "market", "outcome"]], on=["wallet", "market", "outcome"]
        )
        closed_n = closed.groupby("wallet").size()
        frac = (closed_n / one_sided.reindex(closed_n.index)).fillna(0)
        step(
            "closed_before_resolution",
            set(frac.index[frac > cfg.max_closed_before_resolution_ratio]),
        )
    else:
        step("closed_before_resolution", set())

    # 5-7. sample size, span, concentration (on price-band-filtered units)
    step("min_resolved_markets", set(e.index[e["n_markets"] < cfg.min_resolved_markets]))
    upan = u.groupby("wallet")["first_ts"]
    act_days = (upan.max() - upan.min()) / DAY
    step("min_active_span_days", set(act_days.index[act_days < cfg.min_active_span_days]))
    step(
        "single_market_pnl_share",
        set(e.index[e["best_market_pnl_share"] > cfg.max_single_market_pnl_share]),
    )

    flags = (
        wash_flags(trades)
        if cfg.flag_wash_trading
        else pd.DataFrame(columns=["wallet", "flag", "detail"])
    )
    return EligibilityResult(sorted(alive), u, edges, cap, steps, flags)
