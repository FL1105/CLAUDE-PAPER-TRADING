import json

import numpy as np
import pytest
import yaml

from bot.config import load_costs, load_universe
from bot.learn import costs as L

U = load_universe()
C = load_costs()


def _runs(tmp_path, n, slip_bps, cls_symbol="SPY", broker="AlpacaPaperBroker", fee=0.0, seed=0):
    rng = np.random.default_rng(seed)
    for i in range(n):
        d = f"2025-01-{i % 28 + 1:02d}-{i}"  # unique run folders
        ref = 100.0
        s = slip_bps + rng.normal(0, 1.0)
        side = "buy" if i % 2 == 0 else "sell"
        price = ref * (1 + s / 1e4) if side == "buy" else ref / (1 + s / 1e4)
        rec = {"run_id": d, "broker": broker, "fills": [{
            "symbol": cls_symbol, "side": side, "qty": 10.0, "price": price, "ref_price": ref,
            "fee_usd": fee, "status": "filled"}]}
        (tmp_path / "runs" / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / "runs" / d / "run.json").write_text(json.dumps(rec))
    return tmp_path / "runs"


def test_simulator_fills_are_excluded_as_circular(tmp_path):
    runs = _runs(tmp_path, 20, 4.0, broker="PaperLocalBroker")
    assert L.collect_fills(runs, U).empty
    assert len(L.collect_fills(runs, U, include_sim=True)) == 20


def test_slippage_sign_and_size(tmp_path):
    df = L.collect_fills(_runs(tmp_path, 40, 6.0), U)
    m = L.fill_metrics(df)
    assert m["slip_bps"].mean() == pytest.approx(6.0, abs=0.6)  # buys and sells both cost +6 bps
    assert (m["fee_bps"] == 0).all()


def test_detects_higher_than_modelled_cost_and_bounds_it(tmp_path):
    modelled = C.side_bps("equity")  # 4 bps
    df = L.collect_fills(_runs(tmp_path, 80, 3 * modelled), U)  # real costs are 3x the model
    e = {x.cls: x for x in L.estimate(df, C)}["equity"]
    assert e.action == "increase" and e.n == 80
    assert e.ci_lo < e.obs_total_bps < e.ci_hi
    assert modelled < e.proposed_total_bps <= 2.0 * modelled  # shrunk and bounded at 2x
    assert e.proposed_spread_slippage_bps == pytest.approx(e.proposed_total_bps - C.per_side_bps["equity"])
    other = {x.cls: x for x in L.estimate(df, C)}["bond"]
    assert other.action == "insufficient"


def test_decrease_needs_many_fills_and_tight_ci(tmp_path):
    modelled = C.side_bps("equity")
    few = L.collect_fills(_runs(tmp_path / "a", 30, 0.5), U)
    assert {x.cls: x for x in L.estimate(few, C)}["equity"].action == "hold"  # 30 < 60
    many = L.collect_fills(_runs(tmp_path / "b", 120, 0.5), U)
    e = {x.cls: x for x in L.estimate(many, C)}["equity"]
    assert e.action == "decrease" and e.proposed_total_bps >= 0.5 * modelled  # floor at 0.5x


def test_fee_is_added_to_total(tmp_path):
    df = L.collect_fills(_runs(tmp_path, 30, 1.0, fee=0.05), U)  # 0.05 USD on 1000 notional = 0.5 bps
    m = L.fill_metrics(df)
    assert m["fee_bps"].mean() == pytest.approx(0.5, abs=0.01)
    assert m["total_bps"].mean() == pytest.approx(1.5, abs=0.5)


def test_apply_is_conservative_and_logged(tmp_path):
    modelled = C.side_bps("equity")
    est_up = {x.cls: x for x in L.estimate(L.collect_fills(_runs(tmp_path / "u", 80, 3 * modelled), U), C)}
    est_dn = {x.cls: x for x in L.estimate(L.collect_fills(_runs(tmp_path / "d", 120, 0.5, cls_symbol="TLT"), U), C)}
    cfg = tmp_path / "bot" / "config"
    cfg.mkdir(parents=True)
    # unattended mode applies the increase only
    applied = L.apply_estimates([est_up["equity"], est_dn["bond"]], "", True, cfg, "2025-02-01")
    assert [a.cls for a in applied] == ["equity"]
    data = yaml.safe_load((cfg / "costs_learned.yaml").read_text())
    assert data["approved_by"] == "auto-conservative" and "bond" not in data["spread_slippage_bps"]
    # operator mode also applies the decrease
    applied = L.apply_estimates([est_dn["bond"]], "alice", False, cfg, "2025-02-02")
    assert [a.cls for a in applied] == ["bond"]
    data = yaml.safe_load((cfg / "costs_learned.yaml").read_text())
    assert data["approved_by"] == "alice" and set(data["spread_slippage_bps"]) == {"equity", "bond"}
    assert len((tmp_path / "bot" / "state" / "costs_history.jsonl").read_text().splitlines()) == 2


def test_load_costs_merges_only_signed_learned_file(tmp_path, monkeypatch):
    import bot.config as cfgmod
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "costs.yaml").write_text((cfgmod.CFG / "costs.yaml").read_text())
    monkeypatch.setattr(cfgmod, "CFG", cfg)
    base = cfgmod.load_costs()
    (cfg / "costs_learned.yaml").write_text(yaml.safe_dump({"approved_by": "", "spread_slippage_bps": {"equity": 9}}))
    assert cfgmod.load_costs().spread_slippage_bps["equity"] == base.spread_slippage_bps["equity"]  # unsigned: ignored
    (cfg / "costs_learned.yaml").write_text(
        yaml.safe_dump({"approved_by": "bob", "spread_slippage_bps": {"equity": 9}}))
    c2 = cfgmod.load_costs()
    assert c2.spread_slippage_bps["equity"] == 9
    assert c2.spread_slippage_bps["bond"] == base.spread_slippage_bps["bond"]
    monkeypatch.setenv("COSTS_IGNORE_LEARNED", "1")
    assert cfgmod.load_costs().spread_slippage_bps["equity"] == base.spread_slippage_bps["equity"]


def test_order_size_table_includes_fixed_fees():
    c = C.model_copy(update={"fixed_fee_usd_per_order": 1.0})
    t = {r["order_usd"]: r["round_trip_bps"] for r in L.order_size_table(c, "equity")}
    assert t[100] == pytest.approx(2 * C.side_bps("equity") + 200)  # 2 USD on 100 USD = 200 bps
    assert t[2500] < t[100]


def test_calibration_report_renders(tmp_path):
    df = L.collect_fills(_runs(tmp_path, 40, 6.0), U)
    ests = L.estimate(df, C)
    L.calibration_md(ests, df, C, tmp_path / "r" / "cal.md", breakeven={"sharpe_x1": 0.8, "breakeven": 7.3})
    t = (tmp_path / "r" / "cal.md").read_text()
    assert "equity" in t and "7.3x" in t and "WHAT COULD I BE WRONG ABOUT" in t


def test_breakeven_multiplier_on_synthetic_book():
    from bot.config import load_risk
    from bot.data.synthetic import make_prices
    p = make_prices(end="2013-12-31", seed=3, edge_strength=4.0)
    panel = {k: p[k] for k in ("close", "open", "cash", "volume")}
    out = L.breakeven_cost_multiplier(panel, {"trend": {"sma": 100, "vol_window": 60}}, U, load_risk(), C, "next_open",
                                      hi=32.0, iters=4)
    assert "sharpe_x1" in out and out["breakeven"] is not None
    if out["sharpe_x1"] > 0:
        assert out["breakeven"] >= 1.0
