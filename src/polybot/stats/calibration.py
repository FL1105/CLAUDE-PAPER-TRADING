"""Checks that the statistical tests themselves are calibrated.

Assumptions:
    * Under a true null, valid continuous p-values are Uniform(0, 1). We test this
      with a one-sample Kolmogorov-Smirnov test.
    * Rates measured over simulation seeds are binomial; Wilson intervals are
      used because they behave well near 0 and 1.

Failure mode guarded against:
    * A mis-specified test (heavy tails, small samples, a miscalibrated market
      model) that rejects too often under the null. Every downstream FDR and gate
      guarantee assumes the p-values are valid; this module checks that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.stats import kstest, norm


@dataclass(frozen=True)
class KSResult:
    statistic: float
    p_value: float
    n: int


def ks_uniform(p_values: npt.ArrayLike) -> KSResult:
    """One-sample KS test of p-values against Uniform(0, 1)."""
    p = np.asarray(p_values, dtype=np.float64)
    p = p[~np.isnan(p)]
    if p.size == 0:
        raise ValueError("no p-values")
    res = kstest(p, "uniform")
    return KSResult(float(res.statistic), float(res.pvalue), int(p.size))


def wilson_ci(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Two-sided Wilson score interval for a binomial proportion k/n."""
    if n <= 0:
        raise ValueError("n must be > 0")
    if not 0 <= k <= n:
        raise ValueError("need 0 <= k <= n")
    z = float(norm.ppf(0.5 + level / 2))
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    mean_obs: float


def reliability_curve(
    pred: npt.ArrayLike, outcome: npt.ArrayLike, n_bins: int = 10
) -> list[ReliabilityBin]:
    """Bin predicted probabilities and compare with observed frequencies."""
    pr = np.asarray(pred, dtype=np.float64)
    ob = np.asarray(outcome, dtype=np.float64)
    if pr.shape != ob.shape:
        raise ValueError("pred and outcome must have the same shape")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    which = np.clip(np.digitize(pr, edges[1:-1]), 0, n_bins - 1)
    out: list[ReliabilityBin] = []
    for b in range(n_bins):
        m = which == b
        n = int(m.sum())
        out.append(
            ReliabilityBin(
                float(edges[b]),
                float(edges[b + 1]),
                n,
                float(pr[m].mean()) if n else float("nan"),
                float(ob[m].mean()) if n else float("nan"),
            )
        )
    return out
