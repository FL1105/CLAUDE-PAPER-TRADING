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


def idx_prev(world):
    return world["close"].index[-2]


def _env(tmp_path):
    u, c, r = load_universe(), load_costs(), load_risk()
    return u, c, r, PaperLocalBroker(c, u), KillSwitch(tmp_path / "KILL")


def test_orders_queue_after_close_and_fill_at_next_open(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    idx = world["close"].index
    kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "state",
              runs_dir=tmp_path / "runs", tag="SYNTHETIC", rebalance="daily")
    d1 = run_daily(idx[-2], world, br, **kw)
    assert d1.status == "ok" and not br.get_positions(), "nothing may fill on the bar that produced the signal"
    rec1 = json.loads((tmp_path / "runs" / idx[-2].strftime("%Y-%m-%d") / "run.json").read_text())
    assert rec1["queued"] and not rec1["fills"]
    d2 = run_daily(idx[-1], world, br, **kw)
    assert d2.status == "ok" and not d2.mismatches
    fills = [f for f in br.fills.values() if f.status == "filled"]
    assert fills and all(abs(f.price / world["open"][f.symbol].loc[idx[-1]] - 1) < 0.01 for f in fills)


def test_run_trades_towards_targets_and_writes_record(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    idx = world["close"].index
    asof = idx[-1]
    kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "state",
              runs_dir=tmp_path / "runs", tag="SYNTHETIC", rebalance="daily")
    run_daily(idx[-2], world, br, **kw)
    res = run_daily(asof, world, br, **kw)
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
              runs_dir=tmp_path / "runs", rebalance="daily")
    run_daily(idx_prev(world), world, br, **kw)
    run_daily(asof, world, br, **kw)
    pos1, cash1, nfill, npend = br.get_positions(), br.cash, len(br.fills), len(br.pending)
    res2 = run_daily(asof, world, br, **kw)
    assert br.get_positions() == pos1 and br.cash == cash1 and len(br.fills) == nfill
    assert len(br.pending) == npend  # re-planned orders reuse the same client ids: no duplicates
    assert res2.status == "ok"


def test_kill_switch_blocks_orders_and_cancels_waiting_buys(world, tmp_path):
    u, c, r, br, ks = _env(tmp_path)
    idx = world["close"].index
    kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "state",
              runs_dir=tmp_path / "runs", rebalance="daily")
    run_daily(idx[-3], world, br, **kw)
    assert br.pending, "buys are waiting for the open"
    ks.engage("drill", by="pytest")
    res = run_daily(idx[-2], world, br, **kw)
    assert res.status == "killed" and not br.get_positions() and not br.pending
    assert not any(f.status == "filled" for f in br.fills.values())


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
              runs_dir=tmp_path / "runs", rebalance="daily")
    idx = world["close"].index
    run_daily(idx[-20], world, br, **kw)
    # nineteen sessions missed (queued orders fill at the next open we see, then we re-target)
    res = run_daily(idx[-1], world, br, **kw)
    assert res.status == "ok" and not res.mismatches
    res2 = run_daily(idx[-1], world, br, **kw)  # same-day re-run is harmless
    assert res2.status == "ok" and not res2.mismatches


def test_tail_window_equals_full_history_last_row():
    p = make_prices(end="2012-12-31", seed=5)
    close = p["close"]
    u, r = load_universe(), load_risk()
    sel = {"trend": {"sma": 250, "vol_window": 40}, "momentum_rotation": {"lookback_months": 12, "top_n": 3},
           "crypto_trend": {"sma": 200, "vol_cap": 1.2}}
    full, _ = compute_targets(close, sel, u, r)
    tail, _ = compute_targets(close, sel, u, r, tail=daily.LIVE_LOOKBACK_ROWS)
    for d in close.index[-20::4]:
        full_d, _ = compute_targets(close.loc[:d], sel, u, r)
        tail_d, _ = compute_targets(close.loc[:d], sel, u, r, tail=daily.LIVE_LOOKBACK_ROWS)
        pd.testing.assert_series_equal(tail_d.iloc[-1], full_d.iloc[-1], check_exact=False, atol=1e-9)
    pd.testing.assert_series_equal(tail.iloc[-1], full.iloc[-1], check_exact=False, atol=1e-9)


def test_weekly_rebalance_trades_only_on_last_session_of_week(world, tmp_path):
    from bot.data.calendar import nyse_sessions
    from bot.run.daily import is_rebalance_day

    assert is_rebalance_day(pd.Timestamp("2024-06-28"), "weekly")  # Friday
    assert not is_rebalance_day(pd.Timestamp("2024-06-27"), "weekly")
    assert is_rebalance_day(pd.Timestamp("2024-07-03"), "weekly") is False  # Wed; Thu is a holiday? no, Fri is open
    assert is_rebalance_day(pd.Timestamp("2024-07-05"), "weekly")
    assert is_rebalance_day(pd.Timestamp("2024-03-28"), "weekly")  # Good Friday closes: Thursday is the last session
    # matches the engine's mask exactly
    from bot.backtest.engine import rebalance_dates
    idx = nyse_sessions("2023-01-02", "2024-12-31")
    mask = rebalance_dates(idx, "weekly")
    assert all(is_rebalance_day(d, "weekly") == bool(mask[d]) for d in idx[::7])
    u, c, r, br, ks = _env(tmp_path)
    kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "state",
              runs_dir=tmp_path / "runs")
    wed = [d for d in world["close"].index[-60:] if d.weekday() == 2][-1]
    res = run_daily(wed, world, br, **kw)
    assert res.status == "ok" and not br.pending and any("rebalance day" in n for n in res.notes)
