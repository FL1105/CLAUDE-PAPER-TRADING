import numpy as np
import pandas as pd

from bot.backtest.engine import rebalance_dates, run_backtest
from bot.config import load_costs, load_risk, load_universe
from bot.data.calendar import nyse_sessions


def _panel(prices: dict[str, list[float]], opens: dict[str, list[float]] | None = None):
    n = len(next(iter(prices.values())))
    idx = nyse_sessions("2020-01-02", "2021-12-31")[:n]
    close = pd.DataFrame(prices, index=idx)
    open_ = pd.DataFrame(opens, index=idx) if opens else close.copy()
    cash = pd.DataFrame({"BIL": np.full(n, 100.0)}, index=idx)
    return {"close": close, "open": open_, "cash": cash, "volume": close * 0 + 1e9}


FREE = load_costs().model_copy(update={"per_side_bps": dict.fromkeys(load_costs().per_side_bps, 0.0),
                                       "spread_slippage_bps": dict.fromkeys(load_costs().per_side_bps, 0.0)})


def test_known_answer_next_close_zero_cost():
    # Hold 100% SPY from day 0 decision. Fill at close of day 1, so it earns days 2..4.
    px = [100, 110, 121, 133.1, 146.41]
    panel = _panel({"SPY": px})
    tg = pd.DataFrame({"SPY": [1.0] * 5}, index=panel["close"].index)
    res = run_backtest(panel, tg, costs=FREE, exec_mode="next_close", use_ladder=False)
    # day1: weights 0 (decision at day0 executes at close of day1) -> earns nothing
    assert abs(res.net.iloc[0]) < 1e-12
    # days 2..4 each +10% (position held from close of day 1)
    assert np.allclose(res.net.iloc[1:].to_numpy(), 0.10, atol=1e-9)


def test_known_answer_next_open_earns_session_only():
    # open = prior close * 1.02 (gap), close = open * 1.05; hold from day 1 open
    closes = [100.0]
    opens = [100.0]
    for _ in range(4):
        o = closes[-1] * 1.02
        opens.append(o)
        closes.append(o * 1.05)
    panel = _panel({"SPY": closes}, {"SPY": opens})
    tg = pd.DataFrame({"SPY": [1.0] * 5}, index=panel["close"].index)
    res = run_backtest(panel, tg, costs=FREE, exec_mode="next_open", use_ladder=False)
    assert abs(res.net.iloc[0] - 0.05) < 1e-9  # day 1: bought at open, earns session only
    # later days: full gap + session = 1.02*1.05 - 1
    assert np.allclose(res.net.iloc[1:].to_numpy(), 1.02 * 1.05 - 1, atol=1e-9)


def test_cost_difference_is_exactly_modelled():
    px = [100.0] * 6
    panel = _panel({"SPY": px})
    tg = pd.DataFrame({"SPY": [0, 1, 1, 1, 0, 0]}, index=panel["close"].index, dtype=float)
    cm = load_costs().model_copy(update={"per_side_bps": {**load_costs().per_side_bps, "equity": 10.0},
                                         "spread_slippage_bps": {**load_costs().spread_slippage_bps,
                                                                 "equity": 0.0},
                                         "min_order_usd": 0.0})
    free = run_backtest(panel, tg, costs=FREE, use_ladder=False)
    paid = run_backtest(panel, tg, costs=cm, use_ladder=False)
    # entry 100% and exit 100% at 10 bps/side on flat prices
    assert abs(free.net.sum()) < 1e-12
    assert abs(paid.cost.sum() - 2 * 0.0010) < 1e-9
    assert abs((free.net - paid.net).sum() - paid.cost.sum()) < 1e-6
    assert paid.orders.sum() == 2


def test_x2_costs_double_cost():
    px = list(np.linspace(100, 110, 30))
    panel = _panel({"SPY": px})
    tg = pd.DataFrame({"SPY": ([0.0] * 5 + [1.0] * 10) * 2}, index=panel["close"].index)
    a = run_backtest(panel, tg, use_ladder=False, cost_multiplier=1.0)
    b = run_backtest(panel, tg, use_ladder=False, cost_multiplier=2.0)
    assert abs(b.cost.sum() / a.cost.sum() - 2.0) < 1e-6


def test_no_trade_band_and_min_order():
    px = [100.0] * 8
    panel = _panel({"SPY": px})
    w = [0.5, 0.505, 0.5, 0.505, 0.5, 0.505, 0.5, 0.505]
    tg = pd.DataFrame({"SPY": w}, index=panel["close"].index)
    res = run_backtest(panel, tg, use_ladder=False)
    assert res.orders.sum() == 1  # only the initial entry; 0.5% wiggles stay inside the 1% band


def test_determinism_and_lookahead_free():
    from bot.data.synthetic import make_prices
    from bot.strategies.trend import trend_weights

    p = make_prices(end="2009-12-31", seed=2)
    u = load_universe()
    syms = [s for s in u.symbols if s != "BTC/USD" and s != "ETH/USD"]
    panel = {"close": p["close"][syms], "open": p["open"][syms], "cash": p["cash"],
             "volume": p["volume"][syms]}
    w = trend_weights(panel["close"], sma=200, vol_window=60, target_vol=0.10, max_w=0.25)
    reb = rebalance_dates(w.index, "weekly")
    a = run_backtest(panel, w, rebalance=reb)
    b = run_backtest(panel, w, rebalance=reb)
    assert a.net.equals(b.net)
    # perturb everything after day T: results up to T must not change
    T = panel["close"].index[900]
    pert = {k: v.copy() for k, v in panel.items()}
    rng = np.random.default_rng(0)
    for k in ("close", "open"):
        pert[k].loc[pert[k].index > T] *= 1 + rng.normal(0, 0.05, pert[k].loc[pert[k].index > T].shape)
    w2 = trend_weights(pert["close"], sma=200, vol_window=60, target_vol=0.10, max_w=0.25)
    assert w.loc[:T].equals(w2.loc[:T])
    c = run_backtest(pert, w2, rebalance=reb)
    assert np.allclose(a.net.loc[:T].to_numpy(), c.net.loc[:T].to_numpy(), atol=1e-12)


def test_ladder_halves_and_halts():
    n = 120
    px = list(100 * np.cumprod(np.r_[1.0, np.full(n - 1, 0.99)]))  # steady -1%/day
    panel = _panel({"SPY": px})
    tg = pd.DataFrame({"SPY": [1.0] * n}, index=panel["close"].index)
    res = run_backtest(panel, tg, risk=load_risk(), use_ladder=True)
    assert res.mult.min() == 0.0
    assert res.weights["SPY"].max() <= 1.0
    # drawdown must stop at roughly the halt level (plus one day of gap slippage)
    from bot.backtest.metrics import max_drawdown
    assert max_drawdown(res.net.iloc[:40]) < 0.20
