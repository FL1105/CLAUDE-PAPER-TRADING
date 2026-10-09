"""Strategy B: cross-sectional momentum rotation with absolute-momentum filter."""
from __future__ import annotations

import pandas as pd

from bot.strategies.trend import inverse_vol_weights, realised_vol, scale_to_target_vol


def momentum_weights(
    close: pd.DataFrame, lookback_months: int = 12, top_n: int = 4, skip_days: int = 21,
    vol_window: int = 60, target_vol: float = 0.10, max_w: float = 0.25,
) -> pd.DataFrame:
    lb = lookback_months * 21
    mom = close.shift(skip_days) / close.shift(lb) - 1  # return from t-lb to t-skip, causal
    rank = mom.rank(axis=1, ascending=False, method="first")
    picked = ((rank <= top_n) & (mom > 0)).astype(float)
    vol = realised_vol(close, vol_window)
    base = inverse_vol_weights(picked, vol)
    return scale_to_target_vol(base, close, target_vol, vol_window, max_w)


