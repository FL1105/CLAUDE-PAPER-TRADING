from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from polybot.stats.edge import (
    build_units,
    exact_mc_pvalue,
    stake_cap,
    wallet_edges,
    z_statistic,
)


def trades(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        rows, columns=["wallet", "market", "outcome", "side", "price", "shares", "ts"]
    )


WIN = pd.DataFrame({"market": [1, 2, 3, 4], "winning_outcome": [0, 1, 0, np.nan]})


def test_units_aggregate_size_weighted_price_and_ignore_sells() -> None:
    t = trades(
        [
            ("a", 1, 0, "BUY", 0.40, 100, 1000),
            ("a", 1, 0, "BUY", 0.60, 100, 900),
            ("a", 1, 0, "SELL", 0.90, 50, 2000),  # sells ignored (hold to resolution)
        ]
    )
    u = build_units(t, WIN).units
    assert len(u) == 1
    row = u.iloc[0]
    assert row.p_bar == pytest.approx(0.5) and row.shares == 200 and row.cost == pytest.approx(100)
    assert row.y == 1 and row.pnl == pytest.approx(100) and row.first_ts == 900


def test_two_sided_flagged_and_excluded() -> None:
    t = trades(
        [
            ("mm", 1, 0, "BUY", 0.5, 10, 1),
            ("mm", 1, 1, "BUY", 0.5, 10, 2),
            ("mm", 2, 1, "BUY", 0.3, 10, 3),
        ]
    )
    res = build_units(t, WIN)
    assert res.two_sided.to_dict("records") == [{"wallet": "mm", "market": 1}]
    assert res.units["market"].tolist() == [2]


def test_void_and_unknown_markets_excluded() -> None:
    t = trades(
        [
            ("a", 4, 0, "BUY", 0.5, 10, 1),
            ("a", 99, 0, "BUY", 0.5, 10, 1),
            ("a", 2, 0, "BUY", 0.5, 10, 1),
        ]
    )
    u = build_units(t, WIN).units
    assert u["market"].tolist() == [2] and u["y"].tolist() == [0]


def test_missing_columns() -> None:
    with pytest.raises(ValueError):
        build_units(pd.DataFrame({"wallet": ["a"]}), WIN)


def test_z_statistic_by_hand() -> None:
    p = np.array([0.5, 0.2])
    y = np.array([1.0, 0.0])
    w = np.array([10.0, 20.0])
    expect = (10 * 0.5 + 20 * -0.2) / np.sqrt(100 * 0.25 + 400 * 0.16)
    assert z_statistic(p, y, w) == pytest.approx(expect)
    assert z_statistic(np.array([1.0]), np.array([1.0]), np.array([5.0])) == 0.0


def test_stake_cap() -> None:
    u = pd.DataFrame({"cost": np.arange(1, 101, dtype=float)})
    assert stake_cap(u, 0.95) == pytest.approx(np.quantile(np.arange(1, 101), 0.95))
    assert stake_cap(u.iloc[:0], 0.95) == float("inf")


def _units(wallet: str, p: list[float], y: list[int], cost: list[float]) -> pd.DataFrame:
    n = len(p)
    return pd.DataFrame(
        {
            "wallet": wallet,
            "market": range(n),
            "outcome": 0,
            "shares": np.array(cost) / p,
            "cost": cost,
            "p_bar": p,
            "first_ts": np.arange(n) * 86400,
            "y": y,
            "pnl": np.array(cost) / p * np.array(y) - np.array(cost),
        }
    )


def test_wallet_edges_fields_and_cap() -> None:
    rng = np.random.default_rng(0)
    n = 40
    p = rng.uniform(0.2, 0.8, n).round(2).tolist()
    y = (rng.random(n) < 0.6).astype(int).tolist()
    cost = [10.0] * (n - 1) + [1e6]  # one huge bet
    u = _units("a", p, y, cost)
    e = wallet_edges(u, cap=10.0, small_n_exact_threshold=30)
    r = e.iloc[0]
    assert r.n_markets == n and r.n_distinct_days == n and r.p_method == "z"
    # the capped huge bet has the same weight as the others
    assert r.z == pytest.approx(z_statistic(np.array(p), np.array(y, float), np.full(n, 10.0)))
    assert r.p_value == pytest.approx(norm.sf(r.z))
    assert r.roi == pytest.approx(u.pnl.sum() / u.cost.sum())
    assert r.win_rate == pytest.approx(np.mean(y))
    pos = np.maximum(u.pnl, 0)
    assert r.best_market_pnl_share == pytest.approx(pos.max() / pos.sum())


def test_small_n_uses_exact_and_is_deterministic() -> None:
    u = _units("a", [0.3, 0.4, 0.5, 0.6, 0.7], [1, 1, 1, 0, 1], [10.0] * 5)
    e1 = wallet_edges(u, 100.0, small_n_exact_threshold=30, seed=3)
    e2 = wallet_edges(u, 100.0, small_n_exact_threshold=30, seed=3)
    assert e1.iloc[0].p_method == "exact_mc"
    assert e1.equals(e2)
    assert 0 < e1.iloc[0].p_value <= 1


def test_exact_mc_matches_normal_for_large_n() -> None:
    rng = np.random.default_rng(1)
    p = rng.uniform(0.3, 0.7, 400)
    w = np.ones(400)
    z = 1.5
    mc = exact_mc_pvalue(p, w, z, np.random.default_rng(2), 20000)
    assert mc == pytest.approx(norm.sf(z), abs=0.01)
    assert exact_mc_pvalue(np.array([1.0]), np.array([1.0]), 0.0, rng, 10) == 1.0


def test_empty_units() -> None:
    e = wallet_edges(_units("a", [0.5], [1], [1.0]).iloc[:0], 1.0)
    assert e.empty and "z" in e.columns


def test_degenerate_wallet_has_p_one() -> None:
    u = _units("a", [1.0] * 40, [1] * 40, [1.0] * 40)
    r = wallet_edges(u, 10.0).iloc[0]
    assert r.z == 0.0 and r.p_value == 1.0
