"""Statistics that respect how little independent data a backtest holds. All seeded."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats as st

ANN = 252
EULER = 0.5772156649015329


def _sr_daily(x: np.ndarray) -> float:
    sd = x.std(ddof=1)
    return float(x.mean() / sd) if sd > 0 else 0.0


def stationary_bootstrap_indices(n: int, block: float, size: int, rng: np.random.Generator):
    """Politis-Romano stationary bootstrap index matrix (size x n), mean block length `block`."""
    p = 1.0 / block
    starts = rng.integers(0, n, size=(size, n))
    new_block = rng.random((size, n)) < p
    new_block[:, 0] = True
    idx = np.empty((size, n), dtype=np.int64)
    idx[:, 0] = starts[:, 0]
    for j in range(1, n):
        idx[:, j] = np.where(new_block[:, j], starts[:, j], (idx[:, j - 1] + 1) % n)
    return idx


def bootstrap_sharpe_ci(r: pd.Series, n_boot: int = 1000, block: float = 15, seed: int = 7,
                        alpha: float = 0.05, cash: pd.Series | None = None):
    x = (r - (cash.reindex(r.index).fillna(0.0) if cash is not None else 0.0)).to_numpy()
    rng = np.random.default_rng(seed)
    idx = stationary_bootstrap_indices(len(x), block, n_boot, rng)
    s = x[idx]
    sd = s.std(axis=1, ddof=1)
    srs = np.where(sd > 0, s.mean(axis=1) / np.where(sd > 0, sd, 1), 0.0) * np.sqrt(ANN)
    return float(np.quantile(srs, alpha / 2)), float(np.quantile(srs, 1 - alpha / 2))


def psr(r: pd.Series, benchmark_sr_annual: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio (Bailey & Lopez de Prado), daily returns, annual benchmark."""
    x = r.dropna().to_numpy()
    n = len(x)
    if n < 10:
        return float("nan")
    sr = _sr_daily(x)
    g3, g4 = st.skew(x), st.kurtosis(x, fisher=False)
    sr0 = benchmark_sr_annual / np.sqrt(ANN)
    denom = np.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr**2, 1e-12))
    return float(st.norm.cdf((sr - sr0) * np.sqrt(n - 1) / denom))


def expected_max_sharpe(n_trials: int, sr_var_annual: float) -> float:
    """Expected maximum annualised Sharpe among n_trials unskilled trials (Bailey & LdP)."""
    if n_trials <= 1:
        return 0.0
    z1 = st.norm.ppf(1 - 1.0 / n_trials)
    z2 = st.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(sr_var_annual) * ((1 - EULER) * z1 + EULER * z2))


def deflated_sharpe(r: pd.Series, n_trials: int, sr_var_annual: float) -> float:
    return psr(r, benchmark_sr_annual=expected_max_sharpe(n_trials, sr_var_annual))


def min_track_record_years(sr_annual: float, target_sr: float = 0.0, conf: float = 0.95,
                           skew: float = 0.0, kurt: float = 3.0) -> float:
    """Years needed for the Sharpe estimate to exceed target_sr with confidence `conf`."""
    if sr_annual <= target_sr:
        return float("inf")
    sr, sr0 = sr_annual / np.sqrt(ANN), target_sr / np.sqrt(ANN)
    z = st.norm.ppf(conf)
    n = 1 + (1 - skew * sr + (kurt - 1) / 4 * sr**2) * (z / (sr - sr0)) ** 2
    return float(n / ANN)


def paired_bootstrap_diff(a: pd.Series, b: pd.Series, n_boot: int = 1000, block: float = 15,
                          seed: int = 7, metric: str = "sharpe") -> dict:
    """One-sided test that strategy a beats benchmark b. Returns diff, CI and p (a<=b)."""
    j = pd.concat([a, b], axis=1, join="inner").dropna()
    xa, xb = j.iloc[:, 0].to_numpy(), j.iloc[:, 1].to_numpy()
    rng = np.random.default_rng(seed)
    idx = stationary_bootstrap_indices(len(j), block, n_boot, rng)

    def f(m):
        if metric == "mean":
            return m.mean(axis=1) * ANN
        sd = m.std(axis=1, ddof=1)
        return m.mean(axis=1) / np.where(sd > 0, sd, 1) * np.sqrt(ANN)

    d_obs = (f(xa[None, :]) - f(xb[None, :]))[0]
    d = f(xa[idx]) - f(xb[idx])
    # one-sided: how often does a zero-mean (centred) bootstrap difference reach the observed one?
    p = float((np.sum((d - d.mean()) >= d_obs) + 1) / (len(d) + 1))
    return {"diff": float(d_obs), "ci": (float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))),
            "p_value": p}


def placebo_pvalue(actual: float, placebo: np.ndarray) -> float:
    """Fraction of placebo draws at least as good as the real strategy (with +1 smoothing)."""
    return float((np.sum(placebo >= actual) + 1) / (len(placebo) + 1))
