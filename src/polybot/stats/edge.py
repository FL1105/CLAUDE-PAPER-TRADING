"""Per-wallet edge test on (wallet, market) units.

Assumptions:
    * Unit of observation is (wallet, market): several trades by one wallet in one
      market share one thesis and one outcome, so they are aggregated (DECISIONS D1).
    * Only BUY entries into a single outcome token count. A wallet that bought
      both sides of a market is flagged (possible market making) and that market
      is excluded from its edge statistics.
    * Null hypothesis: the market price is calibrated, so for entry price p,
      E[y - p] = 0 and Var[y - p] = p(1 - p) (Bernoulli).
    * Only resolved markets with a known winning outcome enter; void and
      unresolved markets are dropped before this module.

Statistic:
    Z = sum(w_i * e_i) / sqrt(sum(w_i^2 * p_i * (1 - p_i))), one-sided p = 1 - Phi(Z),
    with e_i = y_i - p_i and w_i = min(cost_i, cap).

Failure modes guarded against:
    * Pseudo-replication (counting correlated trades as independent): unit = market.
    * One huge bet dominating: stake weights are capped.
    * Mis-specified normal approximation for small n: wallets with fewer than
      ``small_n_exact_threshold`` units get a seeded Monte-Carlo exact p-value
      simulated directly from the Bernoulli null instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import pandas as pd
from scipy.stats import norm

FloatArr = npt.NDArray[np.float64]

TRADE_COLUMNS = ("wallet", "market", "outcome", "side", "price", "shares", "ts")
UNIT_COLUMNS = (
    "wallet",
    "market",
    "outcome",
    "shares",
    "cost",
    "p_bar",
    "first_ts",
    "y",
    "pnl",
)


@dataclass(frozen=True)
class Units:
    """Aggregated (wallet, market) units plus the flagged two-sided pairs."""

    units: pd.DataFrame
    two_sided: pd.DataFrame  # columns: wallet, market


def build_units(trades: pd.DataFrame, winners: pd.DataFrame) -> Units:
    """Aggregate BUY trades into (wallet, market) units with outcome y in {0, 1}.

    ``trades`` needs TRADE_COLUMNS. ``winners`` has columns ``market`` and
    ``winning_outcome`` (only markets that resolved cleanly; void/ambiguous/open
    markets must not appear, or appear with NaN, and are dropped).
    """
    missing = set(TRADE_COLUMNS) - set(trades.columns)
    if missing:
        raise ValueError(f"trades missing columns: {sorted(missing)}")
    buys = trades.loc[trades["side"] == "BUY", list(TRADE_COLUMNS)]
    n_out = buys.groupby(["wallet", "market"], sort=False)["outcome"].nunique()
    two_sided_idx = n_out[n_out > 1].index
    two_sided = pd.DataFrame(list(two_sided_idx), columns=["wallet", "market"])
    if len(two_sided_idx):
        key = pd.MultiIndex.from_frame(buys[["wallet", "market"]])
        buys = buys.loc[~key.isin(two_sided_idx)]

    buys = buys.assign(cost=buys["price"] * buys["shares"])
    g = buys.groupby(["wallet", "market"], sort=True)
    agg = g.agg(
        outcome=("outcome", "first"),
        shares=("shares", "sum"),
        cost=("cost", "sum"),
        first_ts=("ts", "min"),
    ).reset_index()
    agg = agg.loc[agg["shares"] > 0]
    agg["p_bar"] = agg["cost"] / agg["shares"]  # size-weighted average entry price

    w = winners.dropna(subset=["winning_outcome"])[["market", "winning_outcome"]]
    agg = agg.merge(w, on="market", how="inner")
    agg["y"] = (agg["outcome"] == agg["winning_outcome"]).astype(np.int64)
    agg["pnl"] = agg["shares"] * agg["y"] - agg["cost"]
    units = agg[list(UNIT_COLUMNS)].sort_values(["wallet", "market"]).reset_index(drop=True)
    return Units(units=units, two_sided=two_sided)


def stake_cap(units: pd.DataFrame, quantile: float) -> float:
    """cap = ``quantile`` of unit cost across ALL wallets (computed on selection data only)."""
    if units.empty:
        return float("inf")
    return float(np.quantile(units["cost"].to_numpy(dtype=np.float64), quantile))


def z_statistic(p: FloatArr, y: FloatArr, w: FloatArr) -> float:
    """Z for one wallet. Returns 0.0 when the null variance is zero (degenerate prices)."""
    den = float(np.sum(w * w * p * (1.0 - p)))
    if den <= 0.0:
        return 0.0
    return float(np.sum(w * (y - p)) / np.sqrt(den))


def exact_mc_pvalue(
    p: FloatArr, w: FloatArr, z_obs: float, rng: np.random.Generator, n_sims: int
) -> float:
    """One-sided p-value of ``z_obs`` under y_i ~ Bernoulli(p_i), by seeded simulation.

    Uses (1 + #{Z_sim >= Z_obs}) / (n_sims + 1), which is never 0 and is valid
    (slightly conservative) for discrete statistics.
    """
    den = float(np.sum(w * w * p * (1.0 - p)))
    if den <= 0.0:
        return 1.0
    sims = (rng.random((n_sims, p.size)) < p).astype(np.float64)
    z_sim = ((sims - p) @ w) / np.sqrt(den)
    return float((1 + np.count_nonzero(z_sim >= z_obs - 1e-12)) / (n_sims + 1))


def wallet_edges(
    units: pd.DataFrame,
    cap: float,
    *,
    small_n_exact_threshold: int = 30,
    seed: int = 0,
    n_mc: int = 4000,
) -> pd.DataFrame:
    """Per-wallet edge statistics. One row per wallet, sorted by wallet.

    Columns: wallet, n_markets, n_distinct_days, z, p_value, p_method, roi,
    win_rate, mean_entry_price, best_market_pnl_share, total_cost, total_pnl.
    """
    cols = [
        "wallet",
        "n_markets",
        "n_distinct_days",
        "z",
        "p_value",
        "p_method",
        "roi",
        "win_rate",
        "mean_entry_price",
        "best_market_pnl_share",
        "total_cost",
        "total_pnl",
    ]
    if units.empty:
        return pd.DataFrame(columns=cols)

    u = units.sort_values(["wallet", "market"])
    p = u["p_bar"].to_numpy(dtype=np.float64)
    y = u["y"].to_numpy(dtype=np.float64)
    w = np.minimum(u["cost"].to_numpy(dtype=np.float64), cap)
    df = pd.DataFrame(
        {
            "wallet": u["wallet"].to_numpy(),
            "num": w * (y - p),
            "den": w * w * p * (1.0 - p),
            "y": y,
            "p": p,
            "cost": u["cost"].to_numpy(dtype=np.float64),
            "pnl": u["pnl"].to_numpy(dtype=np.float64),
            "pos_pnl": np.maximum(u["pnl"].to_numpy(dtype=np.float64), 0.0),
            "day": (u["first_ts"].to_numpy() // 86400).astype(np.int64),
        }
    )
    g = df.groupby("wallet", sort=True)
    out = pd.DataFrame(
        {
            "n_markets": g.size(),
            "n_distinct_days": g["day"].nunique(),
            "num": g["num"].sum(),
            "den": g["den"].sum(),
            "win_rate": g["y"].mean(),
            "mean_entry_price": g["p"].mean(),
            "total_cost": g["cost"].sum(),
            "total_pnl": g["pnl"].sum(),
            "pos_sum": g["pos_pnl"].sum(),
            "pos_max": g["pos_pnl"].max(),
        }
    )
    den = out["den"].to_numpy(dtype=np.float64)
    num = out["num"].to_numpy(dtype=np.float64)
    safe = np.where(den > 0, den, 1.0)
    z = np.where(den > 0, num / np.sqrt(safe), 0.0)
    out["z"] = z
    out["p_value"] = np.where(den > 0, norm.sf(z), 1.0)
    out["p_method"] = "z"
    out["roi"] = out["total_pnl"] / out["total_cost"]
    pos_sum = out["pos_sum"].to_numpy(dtype=np.float64)
    out["best_market_pnl_share"] = np.where(
        pos_sum > 0,
        out["pos_max"].to_numpy(dtype=np.float64) / np.where(pos_sum > 0, pos_sum, 1),
        0.0,
    )

    small = out.index[out["n_markets"] < small_n_exact_threshold]
    if len(small):
        rng = np.random.default_rng(seed)
        starts = np.flatnonzero(
            np.r_[True, df["wallet"].to_numpy()[1:] != df["wallet"].to_numpy()[:-1]]
        )
        bounds = dict(
            zip(
                df["wallet"].to_numpy()[starts],
                zip(starts, np.r_[starts[1:], len(df)], strict=True),
                strict=True,
            )
        )
        pv = out["p_value"].to_numpy(dtype=np.float64).copy()
        pos = {wl: i for i, wl in enumerate(out.index)}
        for wl in small:  # sorted order -> deterministic RNG consumption
            a, b = bounds[wl]
            pv[pos[wl]] = exact_mc_pvalue(p[a:b], w[a:b], float(z[pos[wl]]), rng, n_mc)
        out["p_value"] = pv
        out.loc[small, "p_method"] = "exact_mc"

    return out.reset_index()[cols]
