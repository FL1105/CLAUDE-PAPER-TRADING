"""Strategy A: trend + volatility targeting. Pure function of past prices."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.covariance import ledoit_wolf


def trend_signal(close: pd.DataFrame, sma: int) -> pd.DataFrame:
    return (close > close.rolling(sma, min_periods=sma).mean()).astype(float)


def realised_vol(close: pd.DataFrame, window: int, ann: float = 252.0) -> pd.DataFrame:
    return close.pct_change(fill_method=None).rolling(window, min_periods=window).std() * np.sqrt(ann)


def inverse_vol_weights(on: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    iv = (on / vol.where(vol > 0)).fillna(0.0)
    tot = iv.sum(axis=1)
    return iv.div(tot.where(tot > 0), axis=0).fillna(0.0)


def trend_weights(
    close: pd.DataFrame, sma: int = 200, vol_window: int = 60, target_vol: float = 0.10,
    max_w: float = 0.25, cov_window: int | None = None,
) -> pd.DataFrame:
    """Target weights at each close. ON assets get inverse-vol weights, the book is scaled to
    `target_vol` using a Ledoit-Wolf shrunk covariance, gross capped at 1.0 and each weight at
    max_w. Whatever is left over is cash."""
    on = trend_signal(close, sma)
    vol = realised_vol(close, vol_window)
    base = inverse_vol_weights(on, vol)
    return scale_to_target_vol(base, close, target_vol, vol_window if cov_window is None else cov_window, max_w)


def scale_to_target_vol(base: pd.DataFrame, close: pd.DataFrame, target_vol: float,
                        window: int, max_w: float, lev_cap: float = 1.0) -> pd.DataFrame:
    rets = close.pct_change(fill_method=None)
    arr = rets.to_numpy(float)
    b = base.to_numpy(float)
    out = np.zeros_like(b)
    for t in range(len(b)):
        w0 = b[t]
        if t < window or not (w0 > 0).any():
            continue
        w0 = _cap(w0, max_w)  # cap first so risk is measured on what we hold
        cols = np.where(w0 > 0)[0]
        win = arr[t - window + 1 : t + 1][:, cols]
        win = np.nan_to_num(win)
        if len(cols) == 1:
            sig = win.std(ddof=1) * np.sqrt(252)
        else:
            cov = ledoit_wolf(win)[0] * 252
            sig = float(np.sqrt(w0[cols] @ cov @ w0[cols]))
        scale = min(target_vol / sig, lev_cap / w0.sum()) if sig > 0 else 0.0
        w = _cap(w0 * min(scale, 1e6), max_w)
        out[t] = w
    return pd.DataFrame(out, index=base.index, columns=base.columns)


def _cap(w: np.ndarray, max_w: float) -> np.ndarray:
    """Cap each weight at max_w (excess becomes cash; no redistribution) and the sum at 1."""
    w = np.minimum(w, max_w)
    s = w.sum()
    return w / s if s > 1.0 else w
