import numpy as np
import pandas as pd

from bot.config import load_risk, load_strategy, load_universe
from bot.data.calendar import nyse_sessions
from bot.data.synthetic import make_prices
from bot.strategies import make_registry, split_universe
from bot.strategies.crypto_trend import crypto_trend_weights
from bot.strategies.mean_reversion import mean_reversion_weights, rsi
from bot.strategies.momentum_rotation import momentum_weights
from bot.strategies.trend import trend_weights


def _idx(n):
    return nyse_sessions("2015-01-02", "2025-12-31")[:n]


def test_trend_on_in_uptrend_off_in_downtrend():
    n = 500
    up = pd.Series(100 * 1.001 ** np.arange(n), index=_idx(n))
    down = pd.Series(100 * 0.999 ** np.arange(n), index=_idx(n))
    rng = np.random.default_rng(0)
    noise = lambda: 1 + rng.normal(0, 0.004, n)  # noqa: E731
    close = pd.DataFrame({"U": up * noise(), "D": down * noise()})
    w = trend_weights(close, sma=100, vol_window=40, target_vol=0.10, max_w=0.9)
    assert w["U"].iloc[-1] > 0 and w["D"].iloc[-1] == 0
    assert w.sum(axis=1).max() <= 1.0 + 1e-9
    assert (w.iloc[:99] == 0).all().all()  # no signal before the SMA window fills


def test_trend_scales_to_target_vol_and_caps():
    p = make_prices(end="2010-12-31", seed=5)
    u = load_universe()
    core = [s for s in u.symbols if u.cls_of(s) != "crypto"]
    risk = load_risk()
    w = trend_weights(p["close"][core], 200, 60, 0.10, risk.max_weight_per_asset)
    assert w.max().max() <= risk.max_weight_per_asset + 1e-9
    assert w.sum(axis=1).max() <= 1.0 + 1e-9
    rets = p["close"][core].pct_change()
    port = (w.shift(1) * rets).sum(axis=1).iloc[400:]
    realised = port.std() * np.sqrt(252)
    assert 0.03 < realised < 0.16  # near the 10% target, limited by the 1.0 gross cap


def test_momentum_picks_top_n_with_positive_momentum():
    n = 400
    idx = _idx(n)
    cols = {f"A{i}": 100 * np.exp(np.linspace(0, g, n)) for i, g in enumerate([1.0, 0.5, 0.2, -0.3, -0.6])}
    close = pd.DataFrame(cols, index=idx)
    w = momentum_weights(close, lookback_months=6, top_n=2, target_vol=0.5, max_w=0.9, vol_window=40)
    last = w.iloc[-1]
    assert last["A0"] > 0 and last["A1"] > 0 and (last[["A2", "A3", "A4"]] == 0).all()


def test_rsi_extremes():
    idx = _idx(60)
    down = pd.DataFrame({"X": 100 - np.arange(60) * 0.5}, index=idx)
    assert rsi(down).iloc[-1, 0] < 5
    up = pd.DataFrame({"X": 100 + np.arange(60) * 0.5}, index=idx)
    assert rsi(up).iloc[-1, 0] > 95


def test_mean_reversion_enters_on_dip_and_exits():
    n = 320
    idx = _idx(n)
    base = 100 * 1.0008 ** np.arange(n)
    px = base.copy()
    px[300:303] = px[300:303] * np.array([0.97, 0.94, 0.92])  # sharp dip in an uptrend
    for j in range(303, n):
        px[j] = base[j] * 0.93 * 1.01 ** (j - 303)
    close = pd.DataFrame({"X": px}, index=idx)
    w = mean_reversion_weights(close, rsi_entry=10, rsi_exit=60, max_hold_days=5)
    assert w["X"].iloc[:290].sum() == 0
    held_days = (w["X"].iloc[295:] > 0).sum()
    assert 1 <= held_days <= 6  # entered on the dip, left within max_hold_days


def test_crypto_trend_respects_sleeve_cap():
    p = make_prices(end="2012-12-31", seed=1)
    c = p["close"][["BTC/USD", "ETH/USD"]]
    w = crypto_trend_weights(c, sma=100, vol_cap=1.5, sleeve_max=0.10)
    assert w.sum(axis=1).max() <= 0.10 + 1e-9 and w.max().max() <= 0.05 + 1e-9


def test_all_strategies_lookahead_free():
    p = make_prices(end="2010-06-30", seed=7)
    u = load_universe()
    parts = split_universe(p["close"], u)
    reg = make_registry(load_strategy())
    risk = load_risk()
    T = p["close"].index[800]
    for spec in reg.values():
        close = p["close"][parts[spec.universe]]
        params = spec.combos()[0]
        full = spec.build(close, params, risk)
        cut = spec.build(close.loc[:T], params, risk)
        pd.testing.assert_frame_equal(full.loc[:T], cut, check_exact=False, atol=1e-12)
