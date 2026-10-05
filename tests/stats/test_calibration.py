from __future__ import annotations

import numpy as np
import pytest

from polybot.stats.calibration import ks_uniform, reliability_curve, wilson_ci


def test_ks_uniform() -> None:
    rng = np.random.default_rng(0)
    assert ks_uniform(rng.random(5000)).p_value > 0.01
    assert ks_uniform(rng.random(5000) ** 2).p_value < 1e-6
    assert ks_uniform([0.5, np.nan]).n == 1
    with pytest.raises(ValueError):
        ks_uniform([np.nan])


def test_wilson() -> None:
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson_ci(5, 10)
    assert lo < 0.5 < hi
    with pytest.raises(ValueError):
        wilson_ci(1, 0)
    with pytest.raises(ValueError):
        wilson_ci(11, 10)


def test_reliability_curve() -> None:
    rng = np.random.default_rng(1)
    pred = rng.random(20000)
    obs = (rng.random(20000) < pred).astype(float)
    bins = reliability_curve(pred, obs, 10)
    assert len(bins) == 10 and sum(b.n for b in bins) == 20000
    assert all(abs(b.mean_pred - b.mean_obs) < 0.03 for b in bins)
    assert np.isnan(reliability_curve([0.05], [1.0], 10)[5].mean_pred)
    with pytest.raises(ValueError):
        reliability_curve([0.1], [1.0, 0.0])
