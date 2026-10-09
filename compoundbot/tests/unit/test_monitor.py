import json

import numpy as np
import pandas as pd
import pytest

from bot.config import load_strategy
from bot.monitor import drift, weekly_review
from bot.strategies import make_registry


def _r(n, mu, sd, seed=0):
    return pd.Series(np.random.default_rng(seed).normal(mu, sd, n), index=pd.bdate_range("2024-01-01", periods=n))


def test_band_width_is_sampling_noise():
    lo, hi = drift.expected_band(0.7, 60)
    assert (hi - lo) / 2 == pytest.approx(2 * np.sqrt(252 / 60))


def test_drift_ok_for_expected_behaviour_and_cash_for_persistent_failure():
    good = _r(200, 0.05 / 252, 0.10 / np.sqrt(252))
    assert drift.check_drift(good, None, 0.5, 0.10).action == "ok"
    bad = _r(200, -1.5 / 252, 0.10 / np.sqrt(252))  # SR about -15
    st = drift.check_drift(bad, None, 0.5, 0.10, max_days_below=20)
    assert st.action == "cash" and st.days_below_band >= 20


def test_vol_and_tracking_error_alerts():
    paper = _r(200, 0.0003, 0.02)  # vol 32% vs target 10%
    st = drift.check_drift(paper, paper * 0.5, 0.5, 0.10)
    assert st.action in ("alert", "cash") and any("vol" in r for r in st.reasons)
    assert any("tracking" in r for r in st.reasons)
    assert drift.tracking_error_bps(paper, paper) == 0.0


def test_proposals_validated():
    reg = make_registry(load_strategy())
    ok = {"strategy": "trend", "grid": {"sma": [120, 180]}, "rationale": "x"}
    assert weekly_review.validate_proposal(ok, reg)["strategy"] == "trend"
    bad = [
        {"strategy": "trend", "grid": {"sma": [120]}, "risk": {"max_weight_per_asset": 1.0}},
        {"strategy": "nope", "grid": {"sma": [1]}},
        {"strategy": "trend", "grid": {"max_weight": [1]}},
        {"strategy": "trend", "grid": {"sma": "200"}},
        {"strategy": "trend", "grid": {"sma": [1, 2, 3, 4, 5, 6]}},
        {"strategy": "trend", "grid": {"sma": [True]}},
    ]
    for p in bad:
        with pytest.raises(weekly_review.ProposalRejected):
            weekly_review.validate_proposal(p, reg)


def test_queue_caps_at_three_and_logs_rejections(tmp_path):
    reg = make_registry(load_strategy())
    props = [{"strategy": "trend", "grid": {"sma": [120 + i]}} for i in range(5)] + [{"strategy": "bad", "grid": {}}]
    acc = weekly_review.queue_hypotheses(props, reg, tmp_path / "q.jsonl")
    assert len(acc) == 3
    assert len((tmp_path / "q.jsonl").read_text().splitlines()) == 3


def test_challenger_needs_ci_lower_bound_above_zero():
    rng = np.random.default_rng(0)
    base = pd.Series(rng.normal(0.0002, 0.01, 1500), index=pd.bdate_range("2018-01-01", periods=1500))
    better = base + 0.0008
    assert weekly_review.challenger_beats_champion(base, better, base, better)
    assert not weekly_review.challenger_beats_champion(base, better, base, base)  # fails at x2
    noise = base + pd.Series(rng.normal(0, 0.002, 1500), index=base.index)
    assert not weekly_review.challenger_beats_champion(base, noise, base, noise)


def test_weekly_report_from_runs(tmp_path):
    for i, d in enumerate(["2025-03-03", "2025-03-04", "2025-03-05"]):
        (tmp_path / "runs" / d).mkdir(parents=True)
        (tmp_path / "runs" / d / "run.json").write_text(json.dumps({
            "run_id": d, "status": "ok", "ladder": "FULL", "equity": 10000 * (1 + 0.001 * i), "tag": "SYNTHETIC",
            "fills": [{"cost_usd": 0.5}], "mismatches": []}))
    p = weekly_review.write_weekly(tmp_path / "runs", tmp_path / "rep")
    txt = p.read_text()
    assert "Weekly review 2025-03-05" in txt and "SYNTHETIC" in txt and "Ladder: FULL" in txt
    assert "What could I be wrong about" in txt
