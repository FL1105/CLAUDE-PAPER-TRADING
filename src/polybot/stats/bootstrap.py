"""Market-clustered bootstrap for ROI, plus power / minimum-detectable-effect math.

Assumptions:
    * Positions in the same market are correlated (same outcome), so the
      resampling unit is the market (cluster), not the trade.
    * ROI = sum(pnl) / sum(stake) (stake-weighted mean return per dollar).
    * Power formulas use a one-sided normal approximation on per-cluster
      returns with standard deviation ``sigma_r``.

Failure modes guarded against:
    * Overconfident CIs from treating correlated trades as independent.
    * Non-reproducible results: every resample is driven by an explicit seed.
    * "Not significant" being misread as "no edge": the MDE statement says what
      effect size the current sample could actually have detected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.stats import norm

FloatArr = npt.NDArray[np.float64]


@dataclass(frozen=True)
class BootstrapResult:
    roi: float
    lower: float  # one-sided lower bound at ci_level
    ci_level: float
    n_clusters: int
    n_resamples: int
    percentiles: dict[str, float]  # p1, p5, p50, p95, p99 of the bootstrap ROI distribution


def cluster_sums(
    pnl: npt.ArrayLike, stake: npt.ArrayLike, clusters: npt.ArrayLike
) -> tuple[FloatArr, FloatArr]:
    """Sum pnl and stake per cluster (sorted by cluster id, deterministic)."""
    pnl_a = np.asarray(pnl, dtype=np.float64)
    stake_a = np.asarray(stake, dtype=np.float64)
    cl = np.asarray(clusters)
    if not (pnl_a.shape == stake_a.shape == cl.shape):
        raise ValueError("pnl, stake and clusters must have the same shape")
    _, inv = np.unique(cl, return_inverse=True)
    k = int(inv.max()) + 1 if inv.size else 0
    sp = np.bincount(inv, weights=pnl_a, minlength=k).astype(np.float64)
    ss = np.bincount(inv, weights=stake_a, minlength=k).astype(np.float64)
    return sp, ss


def cluster_bootstrap_roi(
    pnl: npt.ArrayLike,
    stake: npt.ArrayLike,
    clusters: npt.ArrayLike,
    *,
    n_resamples: int,
    seed: int,
    ci_level: float = 0.95,
) -> BootstrapResult:
    """One-sided lower confidence bound for ROI by resampling whole clusters."""
    cp, cs = cluster_sums(pnl, stake, clusters)
    k = cp.size
    if k == 0 or float(cs.sum()) <= 0.0:
        raise ValueError("need at least one cluster with positive stake")
    roi = float(cp.sum() / cs.sum())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n_resamples, k))
    num = cp[idx].sum(axis=1)
    den = cs[idx].sum(axis=1)
    dist = np.where(den > 0, num / np.where(den > 0, den, 1.0), 0.0)
    pct = {f"p{q}": float(np.quantile(dist, q / 100)) for q in (1, 5, 50, 95, 99)}
    lower = float(np.quantile(dist, 1.0 - ci_level))
    return BootstrapResult(roi, lower, ci_level, k, n_resamples, pct)


def _z_sum(alpha: float, power: float) -> float:
    if not (0 < alpha < 1 and 0 < power < 1):
        raise ValueError("alpha and power must be in (0, 1)")
    return float(norm.ppf(1 - alpha) + norm.ppf(power))


def minimum_detectable_roi(
    sigma_r: float, n_clusters: int, alpha: float = 0.05, power: float = 0.80
) -> float:
    """Smallest true ROI detectable with the given power: (z_{1-a} + z_pow) * sigma / sqrt(n)."""
    if n_clusters <= 0:
        return math.inf
    if sigma_r < 0:
        raise ValueError("sigma_r must be >= 0")
    return _z_sum(alpha, power) * sigma_r / math.sqrt(n_clusters)


def trades_needed(mu: float, sigma_r: float, alpha: float = 0.05, power: float = 0.80) -> float:
    """Clusters needed to detect true ROI ``mu``: ((z_{1-a} + z_pow) * sigma / mu)^2."""
    if mu <= 0:
        return math.inf
    return (_z_sum(alpha, power) * sigma_r / mu) ** 2


def power_statement(
    sigma_r: float, n_clusters: int, target_roi: float, alpha: float = 0.05, power: float = 0.80
) -> str:
    """Plain-language power summary for the report."""
    mde = minimum_detectable_roi(sigma_r, n_clusters, alpha, power)
    need = trades_needed(target_roi, sigma_r, alpha, power)
    more = max(0, math.ceil(need) - n_clusters) if math.isfinite(need) else None
    more_txt = f"{more} more settled markets" if more is not None else "an unbounded number"
    return (
        f"At your current sample size ({n_clusters} settled markets) you could only detect "
        f"an ROI of {mde:.1%} or better (alpha={alpha}, power={power:.0%}). "
        f"{more_txt.capitalize()} are needed to confirm an edge of {target_roi:.1%}."
    )
