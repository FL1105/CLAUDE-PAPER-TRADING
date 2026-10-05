from __future__ import annotations

from polybot.config import Config
from polybot.gates.ladder import (
    MANUAL_CHECKLIST,
    LiveStats,
    evaluate_micro_live,
    kill_switches,
    ladder,
)
from tests.gates.helpers import DAY, evidence, good_trades


def test_ladder_tiers(cfg: Config) -> None:
    assert ladder(evidence(), cfg).tier == "Rare"
    lr = ladder(evidence(), cfg)
    assert lr.next_tier == "Epic" and lr.blocking_next[0].name == "S3_micro_live"
    d = lr.as_dict()
    assert len(d["manual_checklist"]) == len(MANUAL_CHECKLIST)
    assert not any(c["checked"] for c in d["manual_checklist"])
    bt = ladder(evidence(mode="backtest"), cfg)
    assert bt.tier == "Common" and bt.next_tier == "Uncommon"  # backtest can't pass S0
    assert [g.name for g in bt.blocking_next] == ["1_forward_not_simulated"]
    fw = ladder(evidence(stress_roi=-0.1), cfg)
    assert fw.tier == "Uncommon" and [g.name for g in fw.blocking_next] == ["6_cost_robustness"]
    t = good_trades()
    t["net_pnl"] = -1.0
    none = ladder(evidence(trades=t), cfg)
    assert none.tier is None and none.blocking_next[0].name == "S0_oos_net_edge"


def test_kill_switches(cfg: Config) -> None:
    ks = {k.name: k.triggered for k in kill_switches(evidence(), cfg)}
    assert not any(ks.values()), ks
    t = good_trades()
    t.loc[len(t) - 50 :, "net_pnl"] = -5.06  # last 50 trades all lose
    ks2 = {k.name: k for k in kill_switches(evidence(trades=t), cfg)}
    assert ks2["rolling_roi"].triggered and ks2["drawdown"].triggered
    ks3 = {
        k.name: k.triggered
        for k in kill_switches(
            evidence(epoch_pooled_z=(1.0, -0.1, -0.2), leakage_failures=1),
            cfg,
            modeled_slippage=0.01,
            daily_median_slippage=[0.03, 0.025, 0.021, 0.0],
        )
    }
    assert ks3["oos_z_negative"] and ks3["data_integrity"] and ks3["slippage"]
    few = {k.name: k for k in kill_switches(evidence(trades=good_trades().iloc[:10]), cfg)}
    assert not few["rolling_roi"].triggered and "fewer than" in few["rolling_roi"].detail


def test_micro_live_evaluator(cfg: Config) -> None:
    paper = tuple([0.05, -0.02, 0.1, 0.0] * 50)
    ok = LiveStats(
        fills=120,
        days=31,
        signals=140,
        realized_slippage=0.012,
        modeled_slippage=0.01,
        returns=paper[:120],
    )
    assert all(g.passed for g in evaluate_micro_live(ok, paper, cfg, False))
    bad = LiveStats(
        fills=50,
        days=10,
        signals=100,
        realized_slippage=0.03,
        modeled_slippage=0.01,
        returns=tuple([-0.2] * 50),
    )
    res = {g.name: g.passed for g in evaluate_micro_live(bad, paper, cfg, True)}
    assert not any(res.values()), res
    assert DAY == 86_400
