from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from polybot.gates import metrics as M  # noqa: N812
from tests.gates.helpers import good_trades


def t(
    pnls: list[float],
    markets: list[str] | None = None,
    wallets: list[str] | None = None,
    epochs: list[int] | None = None,
) -> pd.DataFrame:
    n = len(pnls)
    return pd.DataFrame(
        {
            "market": markets or [str(i) for i in range(n)],
            "wallet": wallets or ["w"] * n,
            "stake": [10.0] * n,
            "fee": 0.0,
            "net_pnl": pnls,
            "resolved_ts": list(range(n)),
            "epoch": epochs or [0] * n,
        }
    )


def test_roi_and_empty() -> None:
    assert M.roi(t([5, -10])) == pytest.approx(-0.25)
    assert M.roi(t([]).iloc[:0]) is None


def test_max_drawdown() -> None:
    assert M.max_drawdown(t([10, -30, 5, -10, 40]), 100) == pytest.approx(0.35)
    assert M.max_drawdown(t([-5]), 100) == pytest.approx(0.05)
    assert M.max_drawdown(t([]).iloc[:0], 100) == 0.0


def test_streaks() -> None:
    assert M.longest_losing_streak(np.array([True, False, False, True, False])) == 2
    p99 = M.streak_percentile(500, 0.5, 99, sims=2000, seed=0)
    assert 9 <= p99 <= 14  # longest run of tails in 500 flips ~ log2(500) + a few
    assert M.streak_percentile(0, 0.5, 99, sims=10, seed=0) == 0.0
    assert M.streak_percentile(100, 0.5, 99, sims=500, seed=3) == M.streak_percentile(
        100, 0.5, 99, sims=500, seed=3
    )


def test_concentration_and_top_removal() -> None:
    d = t([100, 10, -5, 5], markets=["a", "b", "c", "a"], wallets=["x", "y", "y", "y"])
    assert M.concentration(d, "market") == pytest.approx(105 / 110)
    assert M.concentration(d, "wallet") == pytest.approx(100 / 110)
    assert M.concentration(t([-1, -2]), "market") == 1.0
    assert M.roi_without_top(d, 1) == pytest.approx(10 / 30)
    assert M.roi_without_top(d, 4) is None


def test_epoch_table_and_rolling() -> None:
    d = t([10, -5, 20, -40], epochs=[0, 0, 1, 1])
    tab = M.epoch_table(d, 1000)
    assert [e["pnl"] for e in tab] == [5, -20] and tab[1]["pnl_frac_bankroll"] == -0.02
    g = good_trades()
    thr = M.rolling_roi_threshold(g, 50, 1.0, sims=2000, seed=0)
    assert thr is not None and thr < M.roi(g)  # type: ignore[operator]
    assert M.rolling_roi_threshold(g.iloc[:10], 50, 1.0, sims=10, seed=0) is None
