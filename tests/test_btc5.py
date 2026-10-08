"""BTC 5-minute favourite-buy analysis on a synthetic recording."""

from __future__ import annotations

from pathlib import Path

import pytest

from polybot.btc5 import analyze, open_db


def _seed(db: Path) -> None:
    con = open_db(db)
    # window A: Up is the favourite (ask .90), Up wins. window B: Down favourite, Up loses.
    for start, up_won, bid, ask in ((0, 1, 0.88, 0.90), (300, 0, 0.08, 0.10)):
        con.execute(
            "INSERT INTO btc5_windows VALUES (?,?,?,?,?)", (start, start + 300, "T", "C", up_won)
        )
        for t in range(start + 200, start + 300, 2):
            con.execute(
                "INSERT INTO btc5_books VALUES (?,?,?,?,?,?)", (start, t, bid, 100, ask, 100)
            )
    con.commit()
    con.close()


def test_favourite_buy_pnl_and_fee(tmp_path: Path) -> None:
    db = tmp_path / "b.sqlite"
    _seed(db)
    r = analyze(db, offset_s=30, lag_s=2, fee_rate=0.07)
    assert r["windows"] == 2 and r["win_rate"] == 1.0
    # A pays the ask 0.90; B buys Down at 1 - bid(0.08) = 0.92; both win
    fee_a, fee_b = 0.07 * 0.9 * 0.1, 0.07 * 0.92 * 0.08
    expected = (0.10 - fee_a + 0.08 - fee_b) / (0.90 + fee_a + 0.92 + fee_b)
    assert r["roi_net_per_dollar"] == pytest.approx(expected)
    assert r["avg_spread"] == pytest.approx(0.02)


def test_stale_or_missing_snapshots_are_skipped(tmp_path: Path) -> None:
    db = tmp_path / "b.sqlite"
    _seed(db)
    assert analyze(db, offset_s=250)["windows"] == 0  # no snapshots that early
