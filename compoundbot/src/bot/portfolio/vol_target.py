"""Whole-book volatility targeting with a Ledoit-Wolf shrunk covariance. Leverage cap 1.0."""
from __future__ import annotations

import pandas as pd

from bot.strategies.trend import scale_to_target_vol


def target_vol_book(weights: pd.DataFrame, close: pd.DataFrame, target_vol: float, window: int,
                    max_w: float, lev_cap: float = 1.0) -> pd.DataFrame:
    """Rescale a combined weight matrix so its estimated vol equals target_vol (never above
    lev_cap gross, never above max_w per asset). Scaling only ever shrinks when vol is too high
    and scales up toward the cap when it is too low."""
    return scale_to_target_vol(weights, close, target_vol, window, max_w, lev_cap)
