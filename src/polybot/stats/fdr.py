"""Benjamini-Hochberg false-discovery-rate control.

Assumptions:
    * p-values are valid (uniform or conservative under the null) and independent
      or positively dependent (PRDS), the conditions under which BH controls FDR.
      Wallets trading the same markets are positively dependent at worst.

Failure mode guarded against:
    * Multiple comparisons: with thousands of wallets, "p < 0.05" selects ~5% of
      them by luck alone. BH caps the expected fraction of false discoveries
      among the selected wallets at ``q`` (DECISIONS D2).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArr = npt.NDArray[np.float64]
BoolArr = npt.NDArray[np.bool_]


@dataclass(frozen=True)
class BHResult:
    reject: BoolArr  # same order as input
    q_values: FloatArr  # BH-adjusted p-values, same order as input
    threshold: float  # largest rejected p-value (0.0 if none)
    n_rejected: int


def benjamini_hochberg(p_values: npt.ArrayLike, q: float) -> BHResult:
    """Step-up BH procedure at FDR level ``q``. NaN p-values are never rejected."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    p = np.asarray(p_values, dtype=np.float64)
    if p.ndim != 1:
        raise ValueError("p_values must be 1-D")
    if np.any((p < 0) | (p > 1)):
        raise ValueError("p-values must lie in [0, 1]")
    valid = ~np.isnan(p)
    m = int(valid.sum())
    reject = np.zeros(p.size, dtype=np.bool_)
    qv = np.full(p.size, np.nan)
    if m == 0:
        return BHResult(reject, qv, 0.0, 0)

    idx = np.flatnonzero(valid)
    pv = p[idx]
    order = np.argsort(pv, kind="mergesort")  # stable -> deterministic ties
    ranked = pv[order]
    ranks = np.arange(1, m + 1, dtype=np.float64)
    below = ranked <= q * ranks / m
    k = int(np.max(np.flatnonzero(below))) + 1 if below.any() else 0

    adj = np.minimum.accumulate((ranked * m / ranks)[::-1])[::-1]
    adj = np.minimum(adj, 1.0)
    q_sorted = np.empty(m)
    q_sorted[order] = adj
    qv[idx] = q_sorted
    if k > 0:
        rej_local = np.zeros(m, dtype=np.bool_)
        rej_local[order[:k]] = True
        reject[idx] = rej_local
    threshold = float(ranked[k - 1]) if k > 0 else 0.0
    return BHResult(reject, qv, threshold, k)
