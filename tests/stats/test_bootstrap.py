from __future__ import annotations

import math

import numpy as np
import pytest

from polybot.stats.bootstrap import (
    cluster_bootstrap_roi,
    cluster_sums,
    minimum_detectable_roi,
    power_statement,
    trades_needed,
)


def test_cluster_sums() -> None:
    cp, cs = cluster_sums([1, 2, -1], [10, 10, 5], ["b", "a", "b"])
    assert cp.tolist() == [2, 0] and cs.tolist() == [10, 15]
    with pytest.raises(ValueError):
        cluster_sums([1], [1, 2], [1])


def test_bootstrap_deterministic_and_bounded() -> None:
    rng = np.random.default_rng(0)
    n = 300
    stake = np.full(n, 10.0)
    pnl = rng.normal(0.5, 10, n)
    cl = rng.integers(0, 120, n)
    a = cluster_bootstrap_roi(pnl, stake, cl, n_resamples=5000, seed=1)
    b = cluster_bootstrap_roi(pnl, stake, cl, n_resamples=5000, seed=1)
    assert a == b
    assert a.lower <= a.roi and a.n_clusters == len(np.unique(cl))
    assert a.percentiles["p1"] <= a.percentiles["p50"] <= a.percentiles["p99"]
    assert a.roi == pytest.approx(pnl.sum() / stake.sum())


def test_clustering_widens_ci() -> None:
    # perfectly correlated trades within a market must not count as independent
    rng = np.random.default_rng(2)
    k, per = 40, 10
    mk_pnl = rng.normal(0, 10, k)
    pnl = np.repeat(mk_pnl, per)
    stake = np.full(k * per, 10.0)
    clustered = cluster_bootstrap_roi(
        pnl, stake, np.repeat(np.arange(k), per), n_resamples=4000, seed=0
    )
    naive = cluster_bootstrap_roi(pnl, stake, np.arange(k * per), n_resamples=4000, seed=0)
    assert clustered.roi - clustered.lower > 2 * (naive.roi - naive.lower)


def test_bootstrap_rejects_empty() -> None:
    with pytest.raises(ValueError):
        cluster_bootstrap_roi([], [], [], n_resamples=10, seed=0)


def test_power_math() -> None:
    mde = minimum_detectable_roi(1.0, 100, 0.05, 0.80)
    assert mde == pytest.approx((1.6448536 + 0.8416212) / 10, rel=1e-6)
    assert trades_needed(mde, 1.0) == pytest.approx(100)
    assert trades_needed(0.0, 1.0) == math.inf
    assert minimum_detectable_roi(1.0, 0) == math.inf
    with pytest.raises(ValueError):
        minimum_detectable_roi(-1.0, 10)
    with pytest.raises(ValueError):
        trades_needed(0.1, 1.0, alpha=0)


def test_power_statement() -> None:
    s = power_statement(1.0, 100, 0.05)
    assert "could only detect an ROI of 24.9%" in s and "2374 more settled markets" in s
    assert "unbounded" in power_statement(1.0, 100, 0.0)
