from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from polybot.stats.fdr import benjamini_hochberg

P = st.lists(st.floats(0.0, 1.0, allow_nan=False), min_size=1, max_size=60)


def test_textbook_example() -> None:
    # Benjamini & Hochberg (1995) 15 p-values, q = 0.05 -> 4 rejections
    p = [
        0.0001,
        0.0004,
        0.0019,
        0.0095,
        0.0201,
        0.0278,
        0.0298,
        0.0344,
        0.0459,
        0.3240,
        0.4262,
        0.5719,
        0.6528,
        0.7590,
        1.0,
    ]
    r = benjamini_hochberg(p, 0.05)
    assert r.n_rejected == 4 and r.reject[:4].all() and not r.reject[4:].any()
    assert r.threshold == 0.0095


def test_step_up_rejects_below_largest_passing_rank() -> None:
    # p_(2)=0.04 fails 0.05*2/3? no: 0.04 <= 0.0333 false, but p_(3)=0.05 <= 0.05 passes
    r = benjamini_hochberg([0.01, 0.04, 0.05], 0.05)
    assert r.reject.all()


def test_nan_and_validation() -> None:
    r = benjamini_hochberg([np.nan, 0.001], 0.1)
    assert r.reject.tolist() == [False, True] and np.isnan(r.q_values[0])
    assert benjamini_hochberg([np.nan], 0.1).n_rejected == 0
    with pytest.raises(ValueError):
        benjamini_hochberg([0.5], 1.0)
    with pytest.raises(ValueError):
        benjamini_hochberg([1.5], 0.1)
    with pytest.raises(ValueError):
        benjamini_hochberg([[0.1]], 0.1)


@given(P, st.floats(0.01, 0.5))
@settings(max_examples=200, deadline=None)
def test_monotone_in_p(p: list[float], q: float) -> None:
    r = benjamini_hochberg(p, q)
    arr = np.asarray(p)
    if r.n_rejected:
        # everything with a p-value <= the largest rejected p-value is rejected
        assert r.reject[arr <= r.threshold].all()
        assert not r.reject[arr > r.threshold].any()


@given(P, st.floats(0.01, 0.3), st.floats(0.0, 0.2))
@settings(max_examples=200, deadline=None)
def test_monotone_in_q(p: list[float], q: float, dq: float) -> None:
    a = benjamini_hochberg(p, q).reject
    b = benjamini_hochberg(p, min(q + dq, 0.99)).reject
    assert not (a & ~b).any()


@given(P, st.floats(0.01, 0.5))
@settings(max_examples=200, deadline=None)
def test_q_values_consistent(p: list[float], q: float) -> None:
    r = benjamini_hochberg(p, q)
    assert np.all(r.q_values >= np.asarray(p) - 1e-12) and np.all(r.q_values <= 1)
    assert np.array_equal(r.reject, r.q_values <= q + 1e-12)


def test_controls_fwer_under_global_null() -> None:
    rng = np.random.default_rng(7)
    q, sims = 0.10, 4000
    any_rej = sum(benjamini_hochberg(rng.random(200), q).n_rejected > 0 for _ in range(sims))
    # under the global null BH's FDR == P(any rejection) == q (independent p-values)
    assert any_rej / sims <= q + 3 * np.sqrt(q * (1 - q) / sims)
