"""Strategy D: crypto majors trend, volatility-capped and sleeve-limited."""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.strategies.trend import realised_vol


def crypto_trend_weights(
    close: pd.DataFrame, sma: int = 200, vol_cap: float = 1.2, vol_window: int = 60,
    sleeve_max: float = 0.10, risk_budget_vol: float = 0.03,
) -> pd.DataFrame:
    """close: crypto columns only (e.g. BTC/USD, ETH/USD) on the shared decision-time calendar.
    ON if close > SMA(sma) and realised vol (annualised with 365) < vol_cap. Weight is sized so each
    asset contributes about risk_budget_vol/n of annual vol, and the sleeve never exceeds sleeve_max."""
    on = (close > close.rolling(sma, min_periods=sma).mean())
    vol = realised_vol(close, vol_window, ann=365.0)
    on &= vol < vol_cap
    n = close.shape[1]
    w = (risk_budget_vol / n) / vol.where(vol > 0)
    w = w.where(on).fillna(0.0).clip(upper=sleeve_max / n)
    tot = w.sum(axis=1)
    over = tot > sleeve_max
    w.loc[over] = w.loc[over].div(tot[over], axis=0) * sleeve_max
    return w.replace([np.inf, -np.inf], 0.0)
