import json

import pandas as pd
import pytest
import yaml

from bot.backtest.walkforward import research
from bot.config import load_budget, load_costs, load_risk, load_strategy, load_universe
from bot.data.synthetic import make_prices
from bot.learn import shadow as sh
from bot.learn.budget import Budget
from bot.learn.cycle import extend_spec, run_cycle
from bot.monitor.weekly_review import pending_proposals, queue_hypotheses
from bot.strategies import make_registry

U, R, C = load_universe(), load_risk(), load_costs()
NOW = "2025-02-03"


def _small_cfg():
    cfg = load_strategy()
    return cfg.model_copy(update={
        "trend": {"sma": [100, 150], "vol_window": [60], "rebalance": "weekly"},
        "walkforward": {**cfg.walkforward, "min_train_years": 3, "test_years": 1}})


@pytest.fixture(scope="module")
def panel():
    p = make_prices(end="2011-12-30", seed=4)
    return {k: p[k] for k in ("close", "open", "cash", "volume")}


def test_extend_spec_unions_and_keeps_order():
    spec = make_registry(load_strategy())["trend"]
    ext = extend_spec(spec, {"sma": [120, 200]})  # 200 already exists
    assert ext.grid["sma"] == [100, 150, 200, 250, 120]
    assert len(ext.combos()) == len(spec.combos()) + len(spec.grid["vol_window"])


def test_cycle_spends_budget_registers_nothing_unvetted_and_defers_when_broke(panel, tmp_path):
    cfg = _small_cfg()
    reg = make_registry(cfg)
    exp, queue, shadows = tmp_path / "exp.jsonl", tmp_path / "q.jsonl", tmp_path / "shadows.json"
    # 1) the pre-registered (founding) research: free
    bcfg = load_budget().model_copy(update={"new_combos_per_quarter": 1})
    research(panel, U, R, C, {"trend": reg["trend"]}, cfg.walkforward, exp, "SYNTHETIC", "d0", n_placebo=3,
             n_boot=40, only=["trend"], budget=Budget(bcfg, exp), now=NOW)
    assert Budget(bcfg, exp).used(NOW) == 0
    # 2) two proposals but budget for one new variant only
    acc = queue_hypotheses([{"strategy": "trend", "grid": {"sma": [120]}, "rationale": "a"},
                            {"strategy": "trend", "grid": {"sma": [130]}, "rationale": "b"}],
                           reg, queue)
    assert len(acc) == 2
    sel = {"trend": {"sma": 100, "vol_window": 60}}
    out = run_cycle(panel, "SYNTHETIC", "d1", U, R, C, cfg, bcfg.model_copy(update={"max_proposals_per_cycle": 3}),
                    exp, queue, shadows, sel, tmp_path / "cycle.md", now=NOW, n_placebo=3, n_boot=40)
    res = [e["result"] for e in out["log"]]
    assert len(res) == 2 and any(r.startswith("deferred") for r in res)  # second one did not fit
    assert out["after"]["used"] == 1 and out["after"]["remaining"] == 0
    left = pending_proposals(queue)
    assert len(left) == 1 and left[0]["grid"] == {"sma": [130]}  # deferred stays queued, evaluated one is closed
    assert "Learning cycle" in (tmp_path / "cycle.md").read_text()
    # whatever happened, nothing registered unless the harness said KEEP with new parameters
    for s in sh.load_shadows(shadows):
        assert s["params"]["sma"] in (120, 130) and s["champion"] == sel["trend"]
    # selected strategies are never touched by a cycle
    assert sel == {"trend": {"sma": 100, "vol_window": 60}}


def test_proposal_for_unselected_strategy_is_closed_without_spending(panel, tmp_path):
    cfg = _small_cfg()
    reg = make_registry(cfg)
    exp, queue = tmp_path / "exp.jsonl", tmp_path / "q.jsonl"
    queue_hypotheses([{"strategy": "momentum_rotation", "grid": {"top_n": [2]}}], reg, queue)
    out = run_cycle(panel, "SYNTHETIC", "d1", U, R, C, cfg, load_budget(), exp, queue, tmp_path / "s.json",
                    {"trend": {"sma": 100, "vol_window": 60}}, tmp_path / "c.md", now=NOW, n_placebo=3, n_boot=40)
    assert "no champion" in out["log"][0]["result"] and out["after"]["used"] == 0
    assert pending_proposals(queue) == []


def test_shadow_waits_for_new_sessions_then_never_promotes_an_identical_challenger(panel, tmp_path):
    bcfg = load_budget().model_copy(update={"min_shadow_days": 30, "shadow_n_boot": 200})
    sel = {"trend": {"sma": 100, "vol_window": 60}}
    through = str(panel["close"].index[-80].date())  # 79 sessions of "new" data
    same = {"id": "t-1", "strategy": "trend", "params": dict(sel["trend"]), "champion": sel["trend"],
            "data_through": through}
    ev = sh.evaluate(panel, same, sel, U, R, C, "next_open", bcfg, "SYNTHETIC")
    assert ev["new_sessions"] == 79 and ev["promotable"] is False  # identical book: zero improvement
    late = {**same, "data_through": str(panel["close"].index[-10].date())}
    ev2 = sh.evaluate(panel, late, sel, U, R, C, "next_open", bcfg, "SYNTHETIC")
    assert ev2["status"] == "pending" and "9/30" in ev2["reason"] or ev2["new_sessions"] < 30
    assert ev2["promotable"] is False


def test_promote_requires_evidence_and_operator_and_is_reversible(tmp_path):
    shadows, selected = tmp_path / "shadows.json", tmp_path / "selected.yaml"
    selected.write_text(yaml.safe_dump(
        {"approved_by": "alice", "strategies": {"trend": {"sma": 100, "vol_window": 60}}}))
    rec = sh.register(shadows, "trend", {"sma": 120, "vol_window": 60}, {"sma": 100, "vol_window": 60},
                      "2025-01-01", "abc12345", NOW)
    kw = dict(shadows_path=shadows, selected_path=selected, backup_dir=tmp_path / "bk", log_path=tmp_path / "p.jsonl")
    with pytest.raises(PermissionError):
        sh.promote(rec["id"], "bob", evaluation={"promotable": False, "reason": "10/126 sessions"}, **kw)
    with pytest.raises(PermissionError):
        sh.promote(rec["id"], "", evaluation={"promotable": True}, **kw)
    assert yaml.safe_load(selected.read_text())["strategies"]["trend"]["sma"] == 100  # untouched so far
    out = sh.promote(rec["id"], "bob", evaluation={"promotable": True, "reason": "ok"}, **kw)
    new = yaml.safe_load(selected.read_text())
    assert new["strategies"]["trend"]["sma"] == 120 and "bob" in new["approved_by"]
    assert yaml.safe_load(open(out["backup"]).read())["strategies"]["trend"]["sma"] == 100  # backup can restore
    assert json.loads((tmp_path / "p.jsonl").read_text())["by"] == "bob"
    with pytest.raises(PermissionError):  # cannot promote twice
        sh.promote(rec["id"], "bob", evaluation={"promotable": True}, **kw)
    assert sh.load_shadows(shadows)[0]["status"] == "promoted"


def test_queue_dedupes_same_idea(tmp_path):
    reg = make_registry(load_strategy())
    q = tmp_path / "q.jsonl"
    a = queue_hypotheses([{"strategy": "trend", "grid": {"sma": [120]}}], reg, q)
    b = queue_hypotheses([{"strategy": "trend", "grid": {"sma": [120]}, "rationale": "again"}], reg, q)
    assert len(a) == 1 and b == [] and len(pending_proposals(q)) == 1
    assert isinstance(pd.Timestamp(NOW), pd.Timestamp)
