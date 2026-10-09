"""Strategy C: short-term mean reversion (long only, liquid ETFs above SMA(200)).

Entry : RSI(2) < rsi_entry and close > SMA(200).
Exit  : RSI(2) > rsi_exit, or held max_hold_days, or close < entry - stop_atr * ATR(14).
ATR is approximated from close-to-close moves (daily high/low are not always available).
High win rate, fat left tail: always inspect the loss distribution, not just the hit rate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.DataFrame, n: int = 2) -> pd.DataFrame:
    d = close.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    # Wilder smoothing
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.where(ad > 0)
    out = 100 - 100 / (1 + rs)
    return out.where(ad > 0, 100.0).where(au.notna())


def atr_proxy(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    return close.diff().abs().rolling(n, min_periods=n).mean()


def mean_reversion_weights(
    close: pd.DataFrame, rsi_entry: float = 10, rsi_exit: float = 60, max_hold_days: int = 5,
    stop_atr: float = 2.5, trend_sma: int = 200, per_position: float = 0.10,
    max_positions: int = 5,
) -> pd.DataFrame:
    r2 = rsi(close, 2).to_numpy(float)
    c = close.to_numpy(float)
    sma = close.rolling(trend_sma, min_periods=trend_sma).mean().to_numpy(float)
    atr = atr_proxy(close).to_numpy(float)
    n, k = c.shape
    held = np.zeros(k, bool)
    age = np.zeros(k, int)
    entry = np.full(k, np.nan)
    stop = np.full(k, np.nan)
    out = np.zeros((n, k))
    for t in range(n):
        for j in range(k):
            if held[j]:
                age[j] += 1
                if r2[t, j] > rsi_exit or age[j] >= max_hold_days or c[t, j] < stop[j]:
                    held[j] = False
        cand = [j for j in range(k)
                if not held[j] and np.isfinite(r2[t, j]) and np.isfinite(sma[t, j])
                and np.isfinite(atr[t, j]) and r2[t, j] < rsi_entry and c[t, j] > sma[t, j]]
        cand.sort(key=lambda j: r2[t, j])
        for j in cand:
            if held.sum() >= max_positions:
                break
            held[j], age[j], entry[j] = True, 0, c[t, j]
            stop[j] = c[t, j] - stop_atr * atr[t, j]
        out[t] = held * per_position
    return pd.DataFrame(out, index=close.index, columns=close.columns)
