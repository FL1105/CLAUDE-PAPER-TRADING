"""Reproducible synthetic worlds that validate the validators.

Worlds:
    * ``null``: every non-market-maker wallet buys a random side of random markets
      at the public price, with no information. Any "skill" found here is a
      false positive by construction.
    * ``skilled``: as null, but a fraction ``skilled_frac`` of wallets observe
      the true probability q with small noise and buy only when their estimate
      beats the public price by ``skilled_threshold``.
    * Both worlds include a market-maker archetype that buys both sides of many
      markets (exercises the two-sided flag) and heavy-tailed activity per wallet.

Price models (config ``synthetic.price_model``; see DECISIONS D10):
    * ``calibrated``: p0 ~ Beta(a, b); q | p0 ~ Beta(p0*k, (1-p0)*k) with k set so
      sd(q | p0) ~= sigma_noise. Then E[y | p0] = p0 exactly: the edge test's
      null hypothesis holds in the null world.
    * ``additive``: q ~ Beta(a, b); p0 = clip(q + N(0, sigma_noise)). This is the
      literal build-spec construction. Prices are then miscalibrated given p0
      (extremes are overconfident), which inflates the true variance of y - p0.

Assumptions:
    * Each market has two outcomes (index 0 = "Yes" priced p0, 1 = "No" priced 1-p0).
    * Timestamps are integer epoch seconds starting at ``T0``.
    * The true probability q is stored for later latency/drift simulation only; no
      selection code may read it (enforced by Phase 3 leakage tests).

Failure mode guarded against:
    * Trusting a pipeline that has never been shown to say "no edge" when there
      is none, or to find edge when there is some. Everything is seeded: the same
      seed gives identical worlds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from polybot.config import SyntheticConfig

T0 = 1_700_000_000
DAY = 86_400
PRICE_LO, PRICE_HI = 0.02, 0.98

World = Literal["null", "skilled"]


@dataclass(frozen=True)
class SyntheticWorld:
    markets: pd.DataFrame  # market, q, p0, y, winning_outcome, open_ts, end_ts
    trades: pd.DataFrame  # wallet, market, outcome, side, price, shares, cost, ts
    wallets: pd.DataFrame  # wallet, archetype in {"noise", "skilled", "market_maker"}
    seed: int
    world: World
    price_model: str

    @property
    def winners(self) -> pd.DataFrame:
        return self.markets[["market", "winning_outcome"]]

    @property
    def wf_markets(self) -> pd.DataFrame:
        """Markets in the walk-forward format (all synthetic markets resolve at end_ts)."""
        m = self.markets[["market", "winning_outcome", "end_ts"]].rename(
            columns={"end_ts": "resolved_ts"}
        )
        return m.assign(status="resolved", winning_outcome=m["winning_outcome"].astype(float))


def _markets(cfg: SyntheticConfig, rng: np.random.Generator, price_model: str) -> pd.DataFrame:
    n = cfg.n_markets
    if price_model == "calibrated":
        p0 = np.clip(rng.beta(cfg.beta_a, cfg.beta_b, n), PRICE_LO, PRICE_HI)
        var = max(cfg.sigma_noise, 1e-9) ** 2
        kappa = np.maximum(p0 * (1 - p0) / var - 1.0, 2.0)
        q = rng.beta(p0 * kappa, (1 - p0) * kappa)
    elif price_model == "additive":
        q = rng.beta(cfg.beta_a, cfg.beta_b, n)
        p0 = np.clip(q + rng.normal(0.0, cfg.sigma_noise, n), PRICE_LO, PRICE_HI)
    else:
        raise ValueError(f"unknown price_model {price_model!r}")
    y = (rng.random(n) < q).astype(np.int64)
    horizon = cfg.horizon_days * DAY
    end_ts = T0 + rng.integers(7 * DAY, horizon, n)
    dur = rng.integers(3 * DAY, 45 * DAY, n)
    open_ts = np.maximum(end_ts - dur, T0)
    return pd.DataFrame(
        {
            "market": np.arange(n, dtype=np.int64),
            "q": q,
            "p0": p0,
            "y": y,
            "winning_outcome": np.where(y == 1, 0, 1).astype(np.int64),
            "open_ts": open_ts.astype(np.int64),
            "end_ts": end_ts.astype(np.int64),
        }
    )


def generate_world(
    cfg: SyntheticConfig,
    seed: int,
    world: World = "null",
    price_model: str | None = None,
) -> SyntheticWorld:
    """Generate one synthetic world. Deterministic in (cfg, seed, world, price_model)."""
    if world not in ("null", "skilled"):
        raise ValueError(f"unknown world {world!r}")
    pm = price_model or cfg.price_model
    rng = np.random.default_rng(seed)
    mk = _markets(cfg, rng, pm)
    n_w, n_m = cfg.n_wallets, cfg.n_markets

    arche = np.full(n_w, "noise", dtype=object)
    perm = rng.permutation(n_w)
    n_mm = round(cfg.market_maker_frac * n_w)
    arche[perm[:n_mm]] = "market_maker"
    if world == "skilled":
        n_sk = round(cfg.skilled_frac * n_w)
        arche[perm[n_mm : n_mm + n_sk]] = "skilled"
    wallet_ids = np.array([f"w{i:05d}" for i in range(n_w)], dtype=object)

    activity = np.ceil(rng.lognormal(cfg.activity_log_mean, cfg.activity_log_sd, n_w))
    activity = np.clip(activity, 1, cfg.max_markets_per_wallet).astype(np.int64)
    mult = np.where(arche == "skilled", cfg.skilled_scan_multiplier, 1.0)
    mult = np.where(arche == "market_maker", 3.0, mult)
    n_draw = np.minimum(np.ceil(activity * mult).astype(np.int64), n_m)

    w_idx = np.repeat(np.arange(n_w), n_draw)
    m_idx = rng.integers(0, n_m, w_idx.size)
    pairs = pd.DataFrame({"w": w_idx, "m": m_idx}).drop_duplicates().to_numpy()
    w_idx, m_idx = pairs[:, 0], pairs[:, 1]
    a = arche[w_idx]

    p0 = mk["p0"].to_numpy()[m_idx]
    q = mk["q"].to_numpy()[m_idx]
    # side choice: noise -> coin flip; skilled -> sign of (q_hat - p0) if beyond threshold
    side_yes = rng.random(w_idx.size) < 0.5
    q_hat = q + rng.normal(0.0, cfg.skilled_obs_noise, w_idx.size)
    edge_yes = q_hat - p0
    sk = a == "skilled"
    take = np.ones(w_idx.size, dtype=bool)
    take[sk] = np.abs(edge_yes[sk]) > cfg.skilled_threshold
    side_yes[sk] = edge_yes[sk] > 0

    is_mm = a == "market_maker"
    rows_w = [w_idx[take & ~is_mm], w_idx[is_mm], w_idx[is_mm]]
    rows_m = [m_idx[take & ~is_mm], m_idx[is_mm], m_idx[is_mm]]
    rows_o = [
        np.where(side_yes[take & ~is_mm], 0, 1),
        np.zeros(int(is_mm.sum()), dtype=np.int64),
        np.ones(int(is_mm.sum()), dtype=np.int64),
    ]
    tw = np.concatenate(rows_w)
    tm = np.concatenate(rows_m)
    to = np.concatenate(rows_o).astype(np.int64)

    # 1-3 fills per position at the public price plus tiny MEAN-ZERO execution noise
    # (a one-sided spread would make null wallets net losers and bias the null test)
    n_fills = 1 + rng.binomial(2, 0.25, tw.size)
    tw, tm, to = np.repeat(tw, n_fills), np.repeat(tm, n_fills), np.repeat(to, n_fills)
    p0_t = mk["p0"].to_numpy()[tm]
    base = np.where(to == 0, p0_t, 1.0 - p0_t)
    price = np.clip(base + rng.uniform(-0.0025, 0.0025, tw.size), PRICE_LO, PRICE_HI)
    cost = rng.lognormal(cfg.stake_log_mean, cfg.stake_log_sd, tw.size) / n_fills.repeat(n_fills)
    open_ts = mk["open_ts"].to_numpy()[tm]
    end_ts = mk["end_ts"].to_numpy()[tm]
    ts = open_ts + (rng.random(tw.size) * (end_ts - open_ts - 3600)).astype(np.int64)

    trades = pd.DataFrame(
        {
            "wallet": wallet_ids[tw],
            "market": tm.astype(np.int64),
            "outcome": to,
            "side": "BUY",
            "price": price,
            "shares": cost / price,
            "cost": cost,
            "ts": ts.astype(np.int64),
        }
    )
    trades = trades.sort_values(["ts", "wallet", "market"], kind="mergesort").reset_index(drop=True)
    wallets = pd.DataFrame({"wallet": wallet_ids, "archetype": arche})
    return SyntheticWorld(mk, trades, wallets, seed, world, pm)
