"""Regression tests for defects found by the adversarial code review."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
import pytest
import yaml

from bot.backtest import stats
from bot.backtest.engine import run_backtest
from bot.backtest.walkforward import run_trials, split_holdout, walk_forward
from bot.broker.alpaca_paper import AlpacaPaperBroker
from bot.broker.paper_local import PaperLocalBroker
from bot.config import load_costs, load_risk, load_strategy, load_universe
from bot.data import panel as P
from bot.data.calendar import nyse_sessions
from bot.data.providers import AlpacaDataProvider
from bot.data.synthetic import make_prices
from bot.learn import costs as LC
from bot.monitor import drift
from bot.monitor.weekly_review import ProposalRejected, validate_proposal
from bot.risk.killswitch import KillSwitch
from bot.run import daily, scheduler
from bot.run.daily import run_daily
from bot.strategies import make_registry

U, R, C = load_universe(), load_risk(), load_costs()
SEL = {"trend": {"sma": 100, "vol_window": 60}}
NY = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def world():
    p = make_prices(end="2009-12-31", seed=21)
    return {k: p[k] for k in ("close", "open", "cash", "volume")}


def _kw(tmp_path, **extra):
    return dict(selected=SEL, universe=U, risk=R, costs=C, kill=KillSwitch(tmp_path / "KILL"),
                state_dir=tmp_path / "state", runs_dir=tmp_path / "runs", rebalance="daily", **extra)


# 1. daily-loss halt must actually fire live, and must not liquidate the book
def test_daily_loss_halt_fires_blocks_buys_and_does_not_liquidate(world, tmp_path):
    br = PaperLocalBroker(C, U)
    idx = world["close"].index
    kw = _kw(tmp_path)
    for d in idx[-8:-4]:
        run_daily(d, world, br, **kw)
    assert br.get_positions()
    before = dict(br.get_positions())
    br.cash -= 0.05 * br.get_account().equity  # a 5% down day arrives between runs
    br.positions = {s: q * 0.0 + q for s, q in br.positions.items()}
    res = run_daily(idx[-4], world, br, **kw)
    assert any("daily loss halt" in n for n in res.notes)
    assert not any(o.side == "buy" for o in br.pending)
    assert br.get_positions() == before  # nothing was liquidated (queued sells, if any, fill next open only)
    assert res.status == "ok"


def test_no_halt_on_a_normal_day(world, tmp_path):
    br = PaperLocalBroker(C, U)
    idx = world["close"].index
    kw = _kw(tmp_path)
    for d in idx[-6:]:
        res = run_daily(d, world, br, **kw)
        assert not any("daily loss halt" in n for n in res.notes)


# 2. staleness is judged against the wall-clock session, not the newest cached bar
def test_last_completed_session():
    f = scheduler.last_completed_session
    assert f(datetime(2024, 7, 1, 9, 0, tzinfo=NY)).date().isoformat() == "2024-06-28"  # Mon morning -> Fri
    assert f(datetime(2024, 7, 1, 17, 0, tzinfo=NY)).date().isoformat() == "2024-07-01"
    assert f(datetime(2024, 7, 6, 12, 0, tzinfo=NY)).date().isoformat() == "2024-07-05"  # Saturday
    assert f(datetime(2024, 7, 4, 20, 0, tzinfo=NY)).date().isoformat() == "2024-07-03"  # holiday


def test_cache_that_ends_before_the_session_is_blocked(world, tmp_path):
    br = PaperLocalBroker(C, U)
    idx = world["close"].index
    stale_asof = nyse_sessions(idx[-1] + pd.Timedelta(days=1), idx[-1] + pd.Timedelta(days=15))[5]
    res = run_daily(stale_asof, world, br, **_kw(tmp_path))
    assert res.status == "data_unhealthy" and not br.pending


# 3. cost decreases need a name, and an unattended increase keeps a human signature
def test_apply_decrease_requires_operator_and_auto_keeps_signature(tmp_path):
    cfg = tmp_path / "b" / "config"
    cfg.mkdir(parents=True)
    est = LC.ClassEstimate("equity", 100, 1, 0, 2, 1, 0, 4, 2, 2, 0, 3, "decrease", "x")
    with pytest.raises(PermissionError):
        LC.apply_estimates([est], "", False, cfg, "2025-01-01")
    LC.apply_estimates([est], "alice", False, cfg, "2025-01-01")
    inc = LC.ClassEstimate("bond", 50, 9, 8, 10, 9, 0, 4.5, 6, 6, 3, 3, "increase", "x")
    LC.apply_estimates([inc], "", True, cfg, "2025-01-02")
    assert yaml.safe_load((cfg / "costs_learned.yaml").read_text())["approved_by"] == "alice"


# 4. a rerun after an early exit still reconciles, and settled fills are not lost or double counted
def test_early_exit_then_rerun_keeps_fills_and_reconciles(world, tmp_path, monkeypatch):
    br = PaperLocalBroker(C, U)
    idx = world["close"].index
    kw = _kw(tmp_path)
    run_daily(idx[-3], world, br, **kw)
    assert br.pending
    # day D: yesterday's orders settle at the open, then the run aborts early (selection file unreadable)
    monkeypatch.setattr(daily, "load_selected", lambda *_: (_ for _ in ()).throw(FileNotFoundError("x")))
    kw_nosel = {k: v for k, v in kw.items() if k != "selected"}
    first = run_daily(idx[-2], world, br, **kw_nosel)
    assert first.status == "no_selection" and br.get_positions()
    # same day, after the fix
    res = run_daily(idx[-2], world, br, **kw)
    assert res.status == "ok" and not res.mismatches
    rec = json.loads((tmp_path / "runs" / idx[-2].strftime("%Y-%m-%d") / "run.json").read_text())
    filled = [f for f in rec["fills"] if f["status"] == "filled"]
    assert filled, "fills settled during the early-exit attempt must survive the rerun"
    assert len({f["client_order_id"] for f in rec["fills"]}) == len(rec["fills"])  # no duplicates
    st = json.loads((tmp_path / "state" / "state.json").read_text())
    assert st["last_run_date"] == idx[-2].strftime("%Y-%m-%d")


# 5. panel on NYSE sessions only; crypto weekend moves compound into the next session
def test_align_to_sessions_removes_weekend_rows_and_keeps_levels():
    days = pd.date_range("2024-06-03", "2024-06-17", freq="D")
    crypto = pd.Series(np.arange(len(days), dtype=float) + 100, index=days)
    sess = nyse_sessions("2024-06-03", "2024-06-17")
    eq = pd.Series(np.arange(len(sess), dtype=float) + 50, index=sess)
    out = P.align_to_sessions({"close": pd.DataFrame({"SPY": eq, "BTC/USD": crypto}),
                               "volume": pd.DataFrame({"SPY": eq * 0 + 1e6, "BTC/USD": crypto * 0 + 5})})
    assert list(out["close"].index) == list(sess)
    assert out["close"].loc["2024-06-10", "BTC/USD"] == crypto.loc["2024-06-10"]  # Monday level, weekend compounded
    assert not out["close"].isna().any().any() and 6 not in out["close"].index.weekday
    assert out["volume"].notna().all().all()


# 6. fixed fees scale with the cost multiplier in stressed backtests
def test_fixed_fees_double_at_two_x(world):
    c = C.model_copy(update={"fixed_fee_usd_per_order": 1.0, "min_order_usd": 0.0})
    cl = world["close"][["SPY", "TLT"]]
    panel = {"close": cl, "open": world["open"][["SPY", "TLT"]], "cash": world["cash"], "volume": None}
    n = len(cl)
    w = pd.DataFrame({"SPY": np.tile([0.0] * 5 + [0.4] * 5, n // 10 + 1)[:n],
                      "TLT": np.tile([0.2] * 5 + [0.0] * 5, n // 10 + 1)[:n]}, index=cl.index)
    a = run_backtest(panel, w, costs=c, use_ladder=False, cost_multiplier=1.0)
    b = run_backtest(panel, w, costs=c, use_ladder=False, cost_multiplier=2.0)
    assert 1.9 * a.cost.sum() < b.cost.sum() < 2.1 * a.cost.sum()  # equity paths differ slightly


# 7. ADV cap uses dollars: a thin ETF with a legitimate order is not blocked, a huge order still is
def test_adv_uses_dollar_volume(world, tmp_path):
    thin = {k: v.copy() for k, v in world.items()}
    thin["volume"] = thin["close"] * 0 + 60_000  # 60k shares/day -> about $6M a day at ~$100
    br = PaperLocalBroker(C, U)
    res = run_daily(thin["close"].index[-2], thin, br, **_kw(tmp_path))
    assert res.status == "ok" and br.pending, "legitimate orders must pass the ADV cap"
    assert not any("ADV" in n for n in res.notes)


# 8. Alpaca partial fills survive cancel/expire; crypto bars use the crypto endpoint; one bad symbol is contained
def test_alpaca_cancelled_after_partial_fill_is_partial():
    f = AlpacaPaperBroker._to_fill({"client_order_id": "x", "symbol": "SPY", "side": "buy",
                                    "filled_qty": "3", "filled_avg_price": "500", "status": "expired"})
    assert f.status == "partial" and f.qty == 3
    g = AlpacaPaperBroker._to_fill({"client_order_id": "x", "symbol": "SPY", "side": "buy",
                                    "filled_qty": "0", "status": "canceled"})
    assert g.status == "rejected"


def test_alpaca_crypto_bars_and_error_containment(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1beta3/crypto/us/bars":
            return httpx.Response(200, json={"bars": {"BTC/USD": [
                {"t": "2024-01-02T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]}})
        if req.url.path == "/v2/stocks/SPY/bars":
            return httpx.Response(200, json={"bars": [
                {"t": "2024-01-02T05:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]})
        return httpx.Response(500)

    prov = AlpacaDataProvider("k", "s", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert prov.bars("BTC/USD", "2024-01-01", "2024-02-01")["close"].iloc[0] == 2
    assert prov.bars("SPY", "2024-01-01", "2024-02-01")["close"].iloc[0] == 2
    with pytest.raises(httpx.HTTPStatusError):
        prov.bars("TLT", "2024-01-01", "2024-02-01")

    class Half:
        tag = "MEASURED"

        def bars(self, s, a, b):
            if s == "TLT":
                raise ValueError("boom")
            return pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
                                index=pd.to_datetime(["2024-01-02"]))

    info = P.fetch_and_cache(Half(), U, "2024-01-01", "2024-02-01", base=tmp_path / "bars")
    kinds = {(f.symbol, f.kind) for f in info["findings"]}
    assert ("TLT", "fetch_error") in kinds  # contained, not fatal
    assert (tmp_path / "bars" / "SPY.parquet").exists()


# 9. paired bootstrap: smoothed one-sided p-value
def test_paired_bootstrap_p_is_smoothed_and_directional():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2018-01-01", periods=1500)
    base = pd.Series(rng.normal(0.0002, 0.01, 1500), index=idx)
    r = stats.paired_bootstrap_diff(base + 0.002, base, n_boot=300, metric="mean")
    assert 0 < r["p_value"] < 0.05  # never exactly zero
    worse = stats.paired_bootstrap_diff(base - 0.002, base, n_boot=300, metric="mean")
    assert worse["p_value"] > 0.9


# 10. proposals: finite, positive, integer-typed like the grid
@pytest.mark.parametrize("grid", [{"sma": [float("nan")]}, {"sma": [-5]}, {"sma": [0]}, {"sma": [120.5]},
                                  {"sma": [float("inf")]}])
def test_bad_proposal_values_rejected(grid):
    with pytest.raises(ProposalRejected):
        validate_proposal({"strategy": "trend", "grid": grid}, make_registry(load_strategy()))


# 11. guards against zero-length windows
def test_walkforward_and_holdout_guards(world):
    with pytest.raises(ValueError):
        split_holdout(world, 0)
    with pytest.raises(ValueError):
        walk_forward({"a": None}, {"a": None}, pd.Series(dtype=float), 4, 0)


# 12. naive datetimes are read in the schedule's timezone consistently
def test_naive_now_is_schedule_timezone():
    a = scheduler.next_run_time(datetime(2024, 7, 1, 9, 0))
    b = scheduler.next_run_time(datetime(2024, 7, 1, 9, 0, tzinfo=NY))
    assert a == b


# 13. drift is wired: persistent shortfall -> CASH_MODE, which the runner honours
def test_drift_cash_mode_end_to_end(world, tmp_path):
    idx = pd.bdate_range("2025-01-01", periods=120)
    bad = pd.DataFrame({"equity": 10000 * np.cumprod(1 - 0.004 + np.random.default_rng(1).normal(0, 0.002, 120))},
                       index=idx)
    short = drift.evaluate_runs(bad.iloc[:30], None, 0.5, 0.10)
    assert short["action"] == "insufficient"
    st = drift.evaluate_runs(bad, None, 0.5, 0.10)
    assert st["action"] == "cash"
    sent = []
    drift.apply_action(st, tmp_path / "state", sent.append)
    assert (tmp_path / "state" / "CASH_MODE").exists() and sent
    br = PaperLocalBroker(C, U)
    run_daily(world["close"].index[-5], world, br, **_kw(tmp_path))
    assert not br.pending, "CASH_MODE must scale all targets to zero (nothing to buy)"
    ok = drift.evaluate_runs(pd.DataFrame({"equity": 10000 * np.cumprod(1 + np.full(120, 0.0004))}, index=idx),
                             None, 0.5, 0.10)
    assert ok["action"] in ("ok", "alert")


# 14. weights are built once for both cost levels
def test_run_trials_builds_weights_once_per_combo(world, monkeypatch):
    spec = make_registry(load_strategy())["trend"]
    calls = []
    orig = spec.build

    def counting(close, p, risk):
        calls.append(1)
        return orig(close, p, risk)

    small = type(spec)(spec.name, {"sma": [100, 150], "vol_window": [60]}, spec.rebalance, spec.universe, counting)
    both, w = run_trials(small, {**world}, U, R, C, "next_open", (1.0, 2.0), "SYNTHETIC")
    assert len(calls) == 2 and set(both) == {1.0, 2.0} and len(w) == 2
    k = next(iter(both[1.0]))
    assert both[2.0][k].cost.sum() > both[1.0][k].cost.sum()
