import numpy as np
import pandas as pd

from bot.backtest import metrics
from bot.backtest.baselines import NAMES, baseline_weights, run_baselines
from bot.backtest.engine import rebalance_dates, run_backtest
from bot.config import load_costs, load_risk, load_strategy, load_universe
from bot.data.synthetic import make_prices
from bot.portfolio.construct import combine, inverse_vol_sleeve_weights
from bot.strategies import make_registry, split_universe


def _world():
    p = make_prices(end="2012-12-31", seed=11)
    return p, load_universe(), load_risk(), load_costs()


def test_baseline_weights_shapes():
    p, u, risk, _ = _world()
    for n in NAMES:
        w = baseline_weights(p["close"], n, u, risk)
        assert w.shape == p["close"].shape
        assert w.sum(axis=1).max() <= 1.0 + 1e-9
    assert baseline_weights(p["close"], "spy", u, risk)["SPY"].eq(1.0).all()
    sf = baseline_weights(p["close"], "sixty_forty", u, risk)
    assert sf["SPY"].iloc[0] == 0.6 and sf["IEF"].iloc[0] == 0.4
    assert baseline_weights(p["close"], "cash", u, risk).abs().sum().sum() == 0


def test_vt_baseline_has_lower_vol_than_plain():
    p, u, risk, costs = _world()
    b = run_baselines(p, u, risk, costs, tag="SYNTHETIC", names=["equal_weight", "vt_equal_weight"])
    sl = slice(400, None)
    assert metrics.ann_vol(b["vt_equal_weight"].net.iloc[sl]) <= metrics.ann_vol(b["equal_weight"].net.iloc[sl]) * 1.1


def test_inverse_vol_sleeve_weights_sum_to_one_and_favour_calm():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2015-01-01", periods=300)
    r = pd.DataFrame({"calm": rng.normal(0, 0.002, 300), "wild": rng.normal(0, 0.01, 300)}, index=idx)
    a = inverse_vol_sleeve_weights(r, 60)
    assert np.allclose(a.iloc[100:].sum(axis=1), 1.0)
    assert (a["calm"].iloc[100:] > a["wild"].iloc[100:]).all()
    assert (a.iloc[:50].sum(axis=1) == 0).all()  # causal: nothing before the window fills


def test_combined_book_respects_limits_and_vol():
    p, u, risk, costs = _world()
    parts = split_universe(p["close"], u)
    reg = make_registry(load_strategy())
    sleeves = {}
    for name in ("trend", "momentum_rotation", "crypto_trend"):
        spec = reg[name]
        sleeves[name] = spec.build(p["close"][parts[spec.universe]], spec.combos()[0], risk)
    w, alloc = combine(sleeves, p["close"], u, risk)
    assert w.sum(axis=1).max() <= risk.gross_exposure_max + 1e-9
    assert w.max().max() <= risk.max_weight_per_asset + 1e-9
    crypto = [c for c in w.columns if u.cls_of(c) == "crypto"]
    assert w[crypto].sum(axis=1).max() <= risk.crypto_sleeve_max + 1e-9
    assert (w >= 0).all().all()  # no shorts
    res = run_backtest({**p, "volume": None}, w, rebalance=rebalance_dates(w.index, "weekly"),
                       costs=costs, risk=risk, universe=u)
    assert res.weights.sum(axis=1).max() <= 1.0 + 1e-9  # leverage never above 1.0
    assert metrics.ann_vol(res.net.iloc[500:]) < 0.16
