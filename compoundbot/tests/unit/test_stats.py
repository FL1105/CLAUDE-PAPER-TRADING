import numpy as np
import pandas as pd
import pytest

from bot.backtest import metrics, stats


def _series(x, start="2015-01-01"):
    return pd.Series(x, index=pd.bdate_range(start, periods=len(x)))


def test_metrics_hand_computed():
    r = _series([0.10, -0.10, 0.05, 0.0])
    assert metrics.max_drawdown(r) == pytest.approx(0.10)
    assert metrics.max_dd_duration(r) >= 2
    eq = 1.1 * 0.9 * 1.05
    assert metrics.cagr(r) == pytest.approx(eq ** (252 / 4) - 1)
    s = metrics.sharpe(r)
    assert s == pytest.approx(r.mean() / r.std(ddof=1) * np.sqrt(252))
    assert metrics.sortino(r) > 0
    # drawdown recovery: -20% needs +25%
    r2 = _series([-0.20, 0.25])
    assert (1 + r2).prod() == pytest.approx(1.0)
    assert metrics.max_drawdown(r2) == pytest.approx(0.20)


def test_sharpe_standard_error_matches_theory():
    # SE of annual Sharpe ~ 1/sqrt(T) for daily data. Check bootstrap CI width ~ 2*1.96*SE.
    rng = np.random.default_rng(1)
    mu, sd = 0.10 / 252, 0.10 / np.sqrt(252)  # SR = 1
    T = 6
    x = _series(rng.normal(mu, sd, int(252 * T)))
    lo, hi = stats.bootstrap_sharpe_ci(x, n_boot=600, block=5, seed=3)
    se_theory = 1 / np.sqrt(T)
    assert (hi - lo) / (2 * 1.96) == pytest.approx(se_theory, rel=0.3)


def test_psr_dsr_and_mintrl():
    rng = np.random.default_rng(2)
    x = _series(rng.normal(0.25 / 252, 0.1 / np.sqrt(252), 252 * 8))
    p = stats.psr(x, 0.0)
    assert 0.9 < p <= 1.0
    # more trials -> higher bar -> lower deflated probability
    d1 = stats.deflated_sharpe(x, 1, 0.25)
    d100 = stats.deflated_sharpe(x, 100, 0.25)
    assert d100 < d1
    # SR=1, normal returns: one-sided 95% needs 1.645^2 = 2.7 years, two-sided 1.96^2 = 3.8
    assert stats.min_track_record_years(1.0, 0.0, 0.95) == pytest.approx(2.7, abs=0.15)
    assert stats.min_track_record_years(1.0, 0.0, 0.975) == pytest.approx(3.85, abs=0.2)
    assert stats.min_track_record_years(0.5, 0.0, 0.95) == pytest.approx(10.8, abs=0.6)
    assert stats.min_track_record_years(0.5, 1.0) == float("inf")
    assert stats.expected_max_sharpe(1, 1.0) == 0.0
    assert stats.expected_max_sharpe(100, 1.0) > stats.expected_max_sharpe(10, 1.0) > 0


def test_bootstrap_is_seeded():
    x = _series(np.random.default_rng(0).normal(0.0003, 0.01, 800))
    a = stats.bootstrap_sharpe_ci(x, seed=5)
    b = stats.bootstrap_sharpe_ci(x, seed=5)
    c = stats.bootstrap_sharpe_ci(x, seed=6)
    assert a == b and a != c


def test_paired_bootstrap_detects_better_and_null():
    rng = np.random.default_rng(3)
    base = rng.normal(0.0002, 0.01, 2000)
    better = base + 0.0008
    a = stats.paired_bootstrap_diff(_series(better), _series(base), n_boot=400, metric="mean")
    assert a["diff"] > 0 and a["p_value"] < 0.05
    n = stats.paired_bootstrap_diff(_series(base + rng.normal(0, 0.002, 2000)), _series(base),
                                    n_boot=400, metric="mean")
    assert n["p_value"] > 0.05


def test_placebo_pvalue():
    assert stats.placebo_pvalue(2.0, np.array([0.1, 0.2, 0.3])) == pytest.approx(1 / 4)
    assert stats.placebo_pvalue(0.0, np.array([0.1, 0.2, 0.3])) == 1.0


def test_stationary_bootstrap_block_length():
    rng = np.random.default_rng(0)
    idx = stats.stationary_bootstrap_indices(2000, block=20, size=50, rng=rng)
    jumps = (np.diff(idx, axis=1) != 1) & (np.diff(idx, axis=1) != -1999)
    mean_block = 1 / jumps.mean()
    assert 15 < mean_block < 27
