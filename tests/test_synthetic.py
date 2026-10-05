from __future__ import annotations

import numpy as np
import pytest

from polybot.config import Config
from polybot.data.synthetic import generate_world
from polybot.stats.calibration import reliability_curve
from polybot.stats.edge import build_units, stake_cap, wallet_edges


def test_deterministic(cfg: Config) -> None:
    a = generate_world(cfg.synthetic, 5, "skilled")
    b = generate_world(cfg.synthetic, 5, "skilled")
    c = generate_world(cfg.synthetic, 6, "skilled")
    assert a.trades.equals(b.trades) and a.markets.equals(b.markets)
    assert not a.trades.equals(c.trades)


def test_bad_args(cfg: Config) -> None:
    with pytest.raises(ValueError):
        generate_world(cfg.synthetic, 1, "weird")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        generate_world(cfg.synthetic, 1, "null", "nope")


def test_trades_well_formed(cfg: Config) -> None:
    w = generate_world(cfg.synthetic, 1, "null")
    t = w.trades.merge(w.markets, on="market")
    assert ((t.ts >= t.open_ts) & (t.ts < t.end_ts)).all()
    assert t.price.between(0.02, 0.98).all() and (t.shares > 0).all()
    assert set(w.wallets.archetype) == {"noise", "market_maker"}


def test_calibrated_prices_are_calibrated(cfg: Config) -> None:
    sc = cfg.synthetic.model_copy(update={"n_markets": 200_000})
    mk = generate_world(sc.model_copy(update={"n_wallets": 10}), 0, "null", "calibrated").markets
    for b in reliability_curve(mk.p0, mk.y, 10):
        if b.n > 1000:
            assert abs(b.mean_pred - b.mean_obs) < 0.01


def test_additive_prices_are_overconfident_at_extremes(cfg: Config) -> None:
    sc = cfg.synthetic.model_copy(update={"n_markets": 200_000, "n_wallets": 10})
    mk = generate_world(sc, 0, "null", "additive").markets
    lo = mk[mk.p0 < 0.1]
    assert lo.y.mean() > lo.p0.mean() + 0.01  # cheap tokens win more often than priced


def test_archetypes_behave(cfg: Config) -> None:
    w = generate_world(cfg.synthetic, 2, "skilled")
    res = build_units(w.trades, w.winners)
    mm = set(w.wallets.loc[w.wallets.archetype == "market_maker", "wallet"])
    assert set(res.two_sided.wallet) == mm  # only market makers buy both sides
    e = wallet_edges(res.units, stake_cap(res.units, 0.95), small_n_exact_threshold=0)
    e = e.merge(w.wallets, on="wallet")
    z = e.groupby("archetype").z.mean()
    assert z["skilled"] > 0.5 and abs(z["noise"]) < 0.15
    assert not np.isin(list(mm), e.wallet).any()
