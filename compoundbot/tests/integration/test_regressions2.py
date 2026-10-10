"""Regression tests for the second adversarial review (broker adapter, order timing, data labelling)."""
import json

import httpx
import numpy as np
import pandas as pd
import pytest

from bot.broker.alpaca_paper import AlpacaPaperBroker, floor_qty, tick_round
from bot.broker.base import Order
from bot.broker.paper_local import PaperLocalBroker
from bot.config import load_costs, load_risk, load_universe
from bot.data import panel as P
from bot.data.calendar import nyse_sessions
from bot.data.providers import AlpacaDataProvider, CsvProvider
from bot.data.synthetic import make_prices
from bot.risk.killswitch import KillSwitch
from bot.run import scheduler
from bot.run.daily import run_daily
from bot.run.orders import DUST_USD, execute, plan_orders

U, R, C = load_universe(), load_risk(), load_costs()
SEL = {"trend": {"sma": 100, "vol_window": 60}}


@pytest.fixture(scope="module")
def world():
    p = make_prices(end="2009-12-31", seed=21)
    return {k: p[k] for k in ("close", "open", "cash", "volume")}


def _kw(tmp_path, **extra):
    return dict(selected=SEL, universe=U, risk=R, costs=C, kill=KillSwitch(tmp_path / "KILL"),
                state_dir=tmp_path / "state", runs_dir=tmp_path / "runs", rebalance="daily", **extra)


# ---- Alpaca order formatting
def test_tick_and_quantity_rounding():
    assert tick_round(412.3456, "buy") == 412.35 and tick_round(412.3456, "sell") == 412.34
    assert tick_round(412.35, "buy") == 412.35 and tick_round(0.123456, "buy") == 0.1235
    assert tick_round(0.123456, "sell") == 0.1234
    assert floor_qty(0.1234567) == 0.123456 and floor_qty(3.0) == 3.0 and floor_qty(1e-7) == 0.0


def test_alpaca_order_body_is_valid_and_symbols_map_back():
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/orders:by_client_order_id":
            return httpx.Response(404)
        if req.url.path == "/v2/orders" and req.method == "POST":
            body = json.loads(req.content)
            sent.append(body)
            return httpx.Response(200, json={"client_order_id": body["client_order_id"], "symbol": "BTCUSD",
                                             "side": body["side"], "filled_qty": body["qty"],
                                             "filled_avg_price": "60000", "status": "filled"})
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "BTCUSD", "qty": "0.5"}, {"symbol": "SPY", "qty": "2"}])
        return httpx.Response(500)

    b = AlpacaPaperBroker(client=httpx.Client(transport=httpx.MockTransport(handler)))
    f = b.submit_order(Order("a", "SPY", "buy", 1.2345678, 412.3456))
    assert sent[0]["limit_price"] == "412.35" and sent[0]["qty"] == "1.234567"
    f2 = b.submit_order(Order("b", "BTC/USD", "sell", 0.1234567, 59900.123))
    assert sent[1]["limit_price"] == "59900.12" and sent[1]["qty"] == "0.123456"
    assert f2.symbol == "BTC/USD" and f.status == "filled"
    assert b.get_positions() == {"BTC/USD": 0.5, "SPY": 2.0}  # keys match the universe
    tiny = b.submit_order(Order("c", "SPY", "sell", 4e-7, 400.0))
    assert tiny.status == "rejected" and len(sent) == 2  # nothing sent for a zero quantity


# ---- crypto bars are labelled by the date the close became known
def test_csv_crypto_dates_shift_but_equities_do_not(tmp_path):
    for name in ("BTC-USD", "SPY"):
        pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "close": [1.0, 2.0]}).to_csv(
            tmp_path / f"{name}.csv", index=False)
    assert list(CsvProvider(tmp_path).bars("BTC/USD", "2024-01-01", "2024-02-01").index.strftime("%m-%d")) == \
        ["01-03", "01-04"]
    assert list(CsvProvider(tmp_path).bars("SPY", "2024-01-01", "2024-02-01").index.strftime("%m-%d")) == \
        ["01-02", "01-03"]
    assert list(CsvProvider(tmp_path, crypto_shift=False).bars("BTC/USD", "2024-01-01", "2024-02-01")
                .index.strftime("%m-%d")) == ["01-02", "01-03"]


def test_alpaca_crypto_data_dates_shift():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bars": {"BTC/USD": [
            {"t": "2024-01-02T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}]}})
    prov = AlpacaDataProvider("k", "s", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert prov.bars("BTC/USD", "2024-01-01", "2024-02-01").index[0] == pd.Timestamp("2024-01-03")


# ---- a drawdown HALT ends flat even if the first flatten sells were lost
def test_halt_reissues_lost_flatten_sells_but_manual_kill_does_not(world, tmp_path):
    br = PaperLocalBroker(C, U)
    idx = world["close"].index
    kw = _kw(tmp_path)
    run_daily(idx[-14], world, br, **kw)
    run_daily(idx[-13], world, br, **kw)
    assert br.get_positions()
    br.cash -= 0.18 * br.get_account().equity
    res = run_daily(idx[-12], world, br, **kw)
    assert res.status == "halted" and br.pending
    br.pending = []  # the flatten sells expired / were rejected: positions are still open
    held = dict(br.get_positions())
    again = run_daily(idx[-11], world, br, **kw)
    assert again.status == "killed" and br.pending and all(o.side == "sell" for o in br.pending)
    run_daily(idx[-10], world, br, **kw)
    assert not br.get_positions() and held

    # a manual kill is different: stop trading entirely, do not sell anything on its own
    br2 = PaperLocalBroker(C, U)
    tmp2 = tmp_path / "m"
    kw2 = _kw(tmp2)
    run_daily(idx[-14], world, br2, **kw2)
    run_daily(idx[-13], world, br2, **kw2)
    kw2["kill"].engage("manual", by="cli")
    run_daily(idx[-12], world, br2, **kw2)
    assert br2.get_positions() and not br2.pending


# ---- stale feed: nothing settles, nothing trades, notes explain why
def test_stale_session_does_not_settle_and_keeps_reason(world, tmp_path):
    br = PaperLocalBroker(C, U)
    idx = world["close"].index
    kw = _kw(tmp_path)
    run_daily(idx[-1], world, br, **kw)
    assert br.pending
    nxt = nyse_sessions(idx[-1] + pd.Timedelta(days=1), idx[-1] + pd.Timedelta(days=10))[0]
    res = run_daily(nxt, world, br, **kw)  # the feed has no bar for this session
    assert res.status == "data_unhealthy"
    assert any("stale or missing data" in n for n in res.notes)
    rec = json.loads((tmp_path / "runs" / nxt.strftime("%Y-%m-%d") / "run.json").read_text())
    assert any("stale or missing data" in n for n in rec["notes"])
    assert not any(f.status == "filled" for f in br.fills.values())  # not filled at an old open


# ---- broker trouble mid-run
class FlakyBroker(PaperLocalBroker):
    def __init__(self, *a, fail_symbol=None, fail_account_after=None, **k):
        super().__init__(*a, **k)
        self.fail_symbol, self.fail_account_after, self.calls = fail_symbol, fail_account_after, 0

    def submit_order(self, order):
        if order.symbol == self.fail_symbol:
            raise ConnectionError("timeout")
        return super().submit_order(order)

    def get_account(self):
        self.calls += 1
        if self.fail_account_after and self.calls > self.fail_account_after:
            raise ConnectionError("account endpoint down")
        return super().get_account()


def test_one_failing_order_does_not_lose_the_others(world, tmp_path):
    idx = world["close"].index
    probe = PaperLocalBroker(C, U)
    run_daily(idx[-1], world, probe, **_kw(tmp_path / "probe"))
    sym = sorted(o.symbol for o in probe.pending)[1]  # a symbol that will be ordered
    br = FlakyBroker(C, U, fail_symbol=sym)
    res = run_daily(idx[-1], world, br, **_kw(tmp_path / "x"))
    assert res.status == "ok" and any("submit error" in n for n in res.notes)
    assert len(br.pending) == len(probe.pending) - 1  # the rest were accepted
    st = json.loads((tmp_path / "x" / "state" / "state.json").read_text())
    assert len(st["pending_ids"]) == len(probe.pending)  # the failed one is re-checked next run


def test_exception_after_submit_still_records_live_orders(world, tmp_path):
    idx = world["close"].index
    br = FlakyBroker(C, U, fail_account_after=1)  # first get_account ok, the post-submit one fails
    with pytest.raises(ConnectionError):
        run_daily(idx[-1], world, br, **_kw(tmp_path))
    st = json.loads((tmp_path / "state" / "state.json").read_text())
    assert st["pending_ids"], "orders already at the broker must be tracked"
    assert (tmp_path / "runs" / idx[-1].strftime("%Y-%m-%d") / "run.json").exists()


def test_execute_contains_errors_and_honours_kill_for_buys(tmp_path):
    ks = KillSwitch(tmp_path / "K")

    class B(PaperLocalBroker):
        def submit_order(self, order):
            if order.symbol == "TLT":
                raise ValueError("boom")
            return super().submit_order(order)

    b = B(C, U)
    b.set_quotes({"SPY": 100.0, "TLT": 100.0, "GLD": 100.0})
    orders = [Order("1", "SPY", "buy", 1, 101), Order("2", "TLT", "buy", 1, 101), Order("3", "GLD", "buy", 1, 101)]
    rep = execute(orders, b, ks)
    assert [f.status for f in rep.fills] == ["open", "open", "open"] and "submit error" in rep.fills[1].reason
    ks.engage("x", by="t")
    from bot.risk.killswitch import KillSwitchEngaged
    with pytest.raises(KillSwitchEngaged):
        execute([Order("4", "SPY", "buy", 1, 101)], b, ks)
    ok = execute([Order("5", "SPY", "sell", 1, 99)], b, ks, reducing_only=True)  # a forced flatten may pass
    assert len(ok.fills) == 1


# ---- data cache honesty
class _Prov:
    tag = "MEASURED"

    def bars(self, s, a, b):
        if s == "SPY":
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])  # empty frame
        if s == "TLT":
            raise ValueError("boom")
        return pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
                            index=pd.to_datetime(["2024-01-02"]))


def test_fetch_clears_old_cache_and_reports_missing(tmp_path):
    base = tmp_path / "bars"
    base.mkdir()
    old = pd.DataFrame({"close": [9.0]}, index=pd.to_datetime(["2020-01-02"]))
    old.to_parquet(base / "SPY.parquet")  # an old SYNTHETIC file
    info = P.fetch_and_cache(_Prov(), U, "2024-01-01", "2024-02-01", base=base)
    assert "SPY" in info["missing"] and "TLT" in info["missing"]
    assert not (base / "SPY.parquet").exists()  # the stale file is gone, so it cannot masquerade as MEASURED
    meta = json.loads((base / "_source.json").read_text())
    assert meta["tag"] == "MEASURED" and "SPY" in meta["missing"]
    with pytest.raises(SystemExit):
        P.load_cached_panel(U, base)  # SPY is required


def test_aligned_series_never_extends_past_the_last_real_bar():
    sess = nyse_sessions("2024-06-03", "2024-06-14")
    a = pd.Series(np.arange(len(sess), dtype=float) + 10, index=sess)
    b = a.iloc[:-3]  # this symbol stopped updating 3 sessions ago
    out = P.align_to_sessions({"close": pd.DataFrame({"A": a, "B": b})})
    assert out["close"]["B"].iloc[-3:].isna().all() and out["close"]["A"].notna().all()
    gap = a.drop(sess[4])  # an interior gap is still filled
    out2 = P.align_to_sessions({"close": pd.DataFrame({"A": a, "G": gap})})
    assert out2["close"]["G"].notna().all()


# ---- dust positions never trigger endless exit orders
def test_dust_position_is_ignored():
    qty = 0.5 * DUST_USD / 100.0
    orders, _ = plan_orders({}, {"SPY": qty}, {"SPY": 100.0}, 10_000.0, R, C, U, "r1")
    assert orders == []
    orders, _ = plan_orders({}, {"SPY": 10.0}, {"SPY": 100.0}, 10_000.0, R, C, U, "r1")
    assert [o.side for o in orders] == ["sell"]


# ---- alerts: a failing webhook is reported as failed
def test_alert_reports_http_errors(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://example.invalid/hook")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(500, request=httpx.Request("POST", a[0])))
    assert scheduler.alert("boom") is False
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, request=httpx.Request("POST", a[0])))
    assert scheduler.alert("fine") is True
