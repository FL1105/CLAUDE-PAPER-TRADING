import json

import pandas as pd
import pytest

from bot.broker.paper_local import PaperLocalBroker
from bot.config import load_costs, load_risk, load_universe
from bot.data.synthetic import make_prices
from bot.risk.killswitch import KillSwitch
from bot.run import daily
from bot.run.daily import compute_targets, run_daily

SEL = {"trend": {"sma": 100, "vol_window": 60}}


@pytest.fixture(scope="module")
def world():
    p = make_prices(end="2009-12-31", seed=21)
    return {"close": p["close"], "open": p["open"], "cash": p["cash"], "volume": p["volume"]}


def _env(tmp_path):
    u, c, r = load_universe(), load_costs(), load_risk()
    return u, c, r, PaperLocalBroker(c, u), KillSwitch(tmp_path / "KILL")


def test_run_trades_towards_targets_and_writes_record(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    asof = world["close"].index[-1]
    res = run_daily(asof, world, br, selected=SEL, universe=u, risk=r, costs=c, kill=ks,
                    state_dir=tmp_path / "state", runs_dir=tmp_path / "runs", tag="SYNTHETIC")
    assert res.status == "ok" and not res.mismatches
    pos = br.get_positions()
    assert pos, "trend should hold something at the end of a long synthetic history"
    acct = br.get_account()
    gross = sum(q * br.quotes[s] for s, q in pos.items()) / acct.equity
    assert gross <= 1.0 and all(q > 0 for q in pos.values())
    rec = json.loads((tmp_path / "runs" / asof.strftime("%Y-%m-%d") / "run.json").read_text())
    assert rec["status"] == "ok" and rec["tag"] == "SYNTHETIC" and rec["inputs_hash"] and rec["fills"]
    assert (tmp_path / "state" / "heartbeat").exists()


def test_second_run_same_day_is_idempotent(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    asof = world["close"].index[-1]
    kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "state",
              runs_dir=tmp_path / "runs")
    run_daily(asof, world, br, **kw)
    pos1, cash1 = br.get_positions(), br.cash
    res2 = run_daily(asof, world, br, **kw)
    assert br.get_positions() == pos1 and br.cash == cash1
    assert not (res2.report and res2.report.fills)


def test_kill_switch_blocks_orders(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    ks.engage("drill", by="pytest")
    res = run_daily(world["close"].index[-1], world, br, selected=SEL, universe=u, risk=r, costs=c,
                    kill=ks, state_dir=tmp_path / "state", runs_dir=tmp_path / "runs")
    assert res.status == "killed" and not br.get_positions()


def test_stale_data_blocks_orders(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    stale = {k: v.copy() for k, v in world.items()}
    stale["close"].loc[stale["close"].index[-6:], "TLT"] = float("nan")
    res = run_daily(world["close"].index[-1], stale, br, selected=SEL, universe=u, risk=r,
                    costs=c, kill=ks, state_dir=tmp_path / "state", runs_dir=tmp_path / "runs")
    assert res.status == "data_unhealthy" and not br.get_positions()


def test_no_selection_means_no_trading(world, tmp_path, monkeypatch):
    u, c, r, br, ks = _env(tmp_path)
    monkeypatch.setattr(daily, "load_selected", lambda *_: (_ for _ in ()).throw(FileNotFoundError("x")))
    res = run_daily(world["close"].index[-1], world, br, universe=u, risk=r, costs=c, kill=ks,
                    state_dir=tmp_path / "state", runs_dir=tmp_path / "runs")
    assert res.status == "no_selection" and not br.get_positions()


def test_replay_of_last_30_days_matches_full_history_targets(world):
    """Dry-run acceptance: targets computed day by day on truncated data == full-history targets."""
    u, r = load_universe(), load_risk()
    full, _ = compute_targets(world["close"], SEL, u, r)
    for d in world["close"].index[-30::5]:
        cut, _ = compute_targets(world["close"].loc[:d], SEL, u, r)
        pd.testing.assert_series_equal(cut.iloc[-1], full.loc[d], check_exact=False, atol=1e-12)


def test_missed_days_recover_cleanly(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "state",
              runs_dir=tmp_path / "runs")
    idx = world["close"].index
    run_daily(idx[-20], world, br, **kw)
    # five sessions missed; next run trades straight to the current target
    res = run_daily(idx[-1], world, br, **kw)
    assert res.status == "ok" and not res.mismatches
