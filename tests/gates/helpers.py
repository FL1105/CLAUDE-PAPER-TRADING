from __future__ import annotations

import numpy as np
import pandas as pd

from polybot.gates.metrics import Evidence

DAY = 86_400


def good_trades(
    n: int = 400, n_markets: int = 200, epochs: int = 6, edge: float = 0.15, seed: int = 0
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    market = np.arange(n) % n_markets
    stake = np.full(n, 5.0)
    p = 0.5
    win = rng.random(n) < p + edge
    pnl = np.where(win, stake / p - stake, -stake) - 0.06
    return pd.DataFrame(
        {
            "market": market.astype(str),
            "wallet": (np.arange(n) % 20).astype(str),
            "stake": stake,
            "fee": 0.06,
            "net_pnl": pnl,
            "resolved_ts": np.arange(n) * 1000,
            "epoch": np.arange(n) * epochs // n,
        }
    )


def evidence(trades: pd.DataFrame | None = None, **kw: object) -> Evidence:
    t = good_trades() if trades is None else trades
    base: dict[str, object] = dict(
        mode="live_forward",
        start_ts=0,
        end_ts=40 * DAY,
        trades=t,
        bankroll=1000.0,
        signals_reached_book=100,
        signals_filled=95,
        median_slippage=0.01,
        pooled_oos_z=3.0,
        frac_wallets_positive_oos=0.7,
        epoch_pooled_z=(1.0, 2.0, 0.5),
        stress_roi=0.05,
        latency_roi={1: 0.1, 2: 0.08, 5: 0.02},
        placebo_rois=tuple(np.linspace(-0.1, 0.05, 200)),
        leakage_failures=0,
        recent_drift_errors=0,
        config_hash_start="h",
        config_hash_now="h",
    )
    base.update(kw)
    return Evidence(**base)  # type: ignore[arg-type]
