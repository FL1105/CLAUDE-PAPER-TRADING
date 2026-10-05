from __future__ import annotations

import pandas as pd
import pytest

from polybot.config import Config
from polybot.gates.gates import all_pass, evaluate
from tests.gates.helpers import DAY, evidence, good_trades


def by_name(cfg: Config, **kw: object) -> dict[str, bool]:
    return {g.name: g.passed for g in evaluate(evidence(**kw), cfg)}


def test_good_evidence_passes_all(cfg: Config) -> None:
    res = evaluate(evidence(), cfg)
    assert all_pass(res), [g.as_dict() for g in res if not g.passed]


def test_gate1_only_live_forward_is_admissible(cfg: Config) -> None:
    assert not by_name(cfg, mode="backtest")["1_forward_not_simulated"]
    assert not by_name(cfg, mode="synthetic")["1_forward_not_simulated"]
    assert not by_name(cfg, end_ts=29 * DAY)["1_forward_not_simulated"]
    sim = {g.name: g.passed for g in evaluate(evidence(mode="synthetic"), cfg, "simulation")}
    assert sim["1_forward_not_simulated"]


def test_gate2_sample_size(cfg: Config) -> None:
    assert not by_name(cfg, trades=good_trades(n=299))["2_sample_size"]
    assert not by_name(cfg, trades=good_trades(n_markets=100))["2_sample_size"]
    assert not by_name(cfg, trades=good_trades(epochs=2))["2_sample_size"]


def test_gate3_needs_ci_lower_bound_above_zero(cfg: Config) -> None:
    weak = good_trades(edge=0.01, seed=1)
    r = {g.name: g for g in evaluate(evidence(trades=weak), cfg)}["3_net_edge_credible"]
    assert not r.passed and r.observed["ci_lower"] < 0
    assert not by_name(cfg, trades=good_trades().iloc[:0])["3_net_edge_credible"]


def test_gate4_placebo(cfg: Config) -> None:
    assert not by_name(cfg, placebo_rois=None)["4_selection_adds_value"]
    assert not by_name(cfg, placebo_rois=tuple([0.5] * 200))["4_selection_adds_value"]


def test_gate5_to_7(cfg: Config) -> None:
    assert not by_name(cfg, pooled_oos_z=1.6)["5_persistence"]
    assert not by_name(cfg, frac_wallets_positive_oos=0.5)["5_persistence"]
    assert not by_name(cfg, pooled_oos_z=None)["5_persistence"]
    assert not by_name(cfg, stress_roi=-0.01)["6_cost_robustness"]
    assert not by_name(cfg, stress_roi=None)["6_cost_robustness"]
    assert not by_name(cfg, latency_roi={1: 0.1, 2: -0.01, 5: -0.1})["7_latency_robustness"]
    assert by_name(cfg, latency_roi={1: 0.1, 2: 0.01, 5: -0.1})["7_latency_robustness"]
    assert not by_name(cfg, latency_roi={1: 0.1})["7_latency_robustness"]


def test_gate8_lucky_streak(cfg: Config) -> None:
    t = good_trades()
    t.loc[0, "net_pnl"] = 10_000.0  # one monster win dominates PnL
    assert not by_name(cfg, trades=t)["8_not_a_lucky_streak"]


def test_gate9_fill(cfg: Config) -> None:
    assert not by_name(cfg, signals_filled=84)["9_fillability"]
    assert not by_name(cfg, median_slippage=0.03)["9_fillability"]
    assert not by_name(cfg, signals_reached_book=0)["9_fillability"]


def test_gate10_drawdown_and_streak(cfg: Config) -> None:
    t = good_trades()
    t.loc[100:160, "net_pnl"] = -5.06  # 61 losses in a row: drawdown ~31% and absurd streak
    r = {g.name: g for g in evaluate(evidence(trades=t), cfg)}["10_drawdown"]
    assert not r.passed and r.observed["max_drawdown"] > 0.2
    assert r.observed["longest_losing_streak"] > r.threshold["streak_p99"]


def test_gate11_stability(cfg: Config) -> None:
    t = good_trades()
    t.loc[t.epoch.isin([0, 1, 2]), "net_pnl"] = -0.5
    assert not by_name(cfg, trades=t)["11_stability"]


def test_gate12_integrity(cfg: Config) -> None:
    assert not by_name(cfg, leakage_failures=1)["12_data_integrity"]
    assert not by_name(cfg, recent_drift_errors=3)["12_data_integrity"]
    assert not by_name(cfg, config_hash_now="changed")["12_data_integrity"]


def test_results_serialisable(cfg: Config) -> None:
    import json

    json.dumps([g.as_dict() for g in evaluate(evidence(), cfg)], default=str)
    assert isinstance(
        evaluate(evidence(trades=pd.DataFrame(columns=good_trades().columns)), cfg)[2].passed, bool
    )


@pytest.mark.parametrize("bad", [{"trades": good_trades().iloc[:0]}])
def test_empty_evidence_fails_everything_but_admin(cfg: Config, bad: dict) -> None:
    names = [n for n, ok in by_name(cfg, **bad).items() if ok]
    assert set(names) <= {
        "1_forward_not_simulated",
        "5_persistence",
        "6_cost_robustness",
        "7_latency_robustness",
        "9_fillability",
        "12_data_integrity",
    }


def test_passed_is_plain_bool(cfg: Config) -> None:
    import json

    for g in evaluate(evidence(), cfg):
        assert type(g.passed) is bool
    json.dumps({g.name: g.passed for g in evaluate(evidence(), cfg)})
