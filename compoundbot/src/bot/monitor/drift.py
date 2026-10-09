"""Drift monitoring: rolling realised Sharpe/vol vs the backtest-expected band, paper-vs-replay
tracking error, and the automatic switch to cash when results stay below the band."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ANN = 252


def rolling_stats(r: pd.Series, window: int = 60) -> pd.DataFrame:
    mu, sd = r.rolling(window).mean(), r.rolling(window).std(ddof=1)
    return pd.DataFrame({"sharpe": mu / sd.where(sd > 0) * np.sqrt(ANN), "vol": sd * np.sqrt(ANN)})


def expected_band(expected_sharpe: float, window: int = 60, z: float = 2.0) -> tuple[float, float]:
    """Sampling band for a rolling Sharpe: SE of an annualised Sharpe over `window` days ~ sqrt(252/window)."""
    se = np.sqrt(ANN / window)
    return expected_sharpe - z * se, expected_sharpe + z * se


def tracking_error_bps(paper: pd.Series, replay: pd.Series) -> float:
    j = pd.concat([paper, replay], axis=1, join="inner").dropna()
    if len(j) < 2:
        return float("nan")
    return float((j.iloc[:, 0] - j.iloc[:, 1]).std(ddof=1) * 1e4)


@dataclass
class DriftStatus:
    action: str  # ok | alert | cash
    reasons: list[str]
    days_below_band: int
    vol_ratio: float
    tracking_error_bps: float


def check_drift(paper: pd.Series, replay: pd.Series | None, expected_sharpe: float, target_vol: float,
                window: int = 60, max_days_below: int = 20, te_limit_bps: float = 15.0) -> DriftStatus:
    reasons: list[str] = []
    rs = rolling_stats(paper, window).dropna()
    lo, _hi = expected_band(expected_sharpe, window)
    below = (rs["sharpe"] < lo)
    run = 0
    for v in below.to_numpy()[::-1]:
        if not v:
            break
        run += 1
    vol_ratio = float(rs["vol"].iloc[-1] / target_vol) if len(rs) else float("nan")
    te = tracking_error_bps(paper, replay) if replay is not None else float("nan")
    action = "ok"
    if run >= max_days_below:
        action = "cash"
        reasons.append(f"rolling {window}d Sharpe below band ({lo:.2f}) for {run} days")
    if np.isfinite(vol_ratio) and not (0.75 <= vol_ratio <= 1.25):
        action = "alert" if action == "ok" else action
        reasons.append(f"realised vol is {vol_ratio:.0%} of target")
    if np.isfinite(te) and te > te_limit_bps:
        action = "alert" if action == "ok" else action
        reasons.append(f"tracking error {te:.1f} bps > {te_limit_bps}")
    return DriftStatus(action, reasons, run, vol_ratio, te)
