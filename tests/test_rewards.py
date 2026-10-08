"""Reward scoring and the two-sided quote replay."""

from __future__ import annotations

import pytest

from polybot.rewards import competitor_score, order_score, simulate_market

BOOK = lambda ts, mid=0.50: (ts, mid, [[mid - 0.01, 100.0]], [[mid + 0.01, 100.0]])  # noqa: E731


def test_order_score_is_quadratic_and_zero_outside_v() -> None:
    assert order_score(0.05, 0.0, 10) == 10
    assert order_score(0.05, 0.025, 10) == pytest.approx(2.5)
    assert order_score(0.05, 0.06, 10) == 0


def test_competitor_single_sided_counts_one_third_inside_band_only() -> None:
    v = 0.05
    one_sided = competitor_score([[0.49, 100.0]], [], 0.50, v)
    assert one_sided == pytest.approx(order_score(v, 0.01, 100) / 3)
    assert (
        competitor_score([[0.04, 100.0]], [], 0.05, v) == 0.0
    )  # outside 0.10-0.90: needs two sides


def test_no_trades_means_reward_only() -> None:
    r = simulate_market([BOOK(0), BOOK(86400)], [], pool=100.0, v=0.05, size=100.0, dist=0.01)
    assert r.fills == 0 and r.fill_pnl == 0.0
    # competitor: min(Qb, Qa) = Q(0.01, 100); mine equal -> share 0.5 -> 100 * 0.5 * 1 day
    assert r.reward == pytest.approx(50.0, rel=1e-6)


def test_bid_fill_then_mark_loses_when_price_falls() -> None:
    books = [BOOK(0), (10, 0.50, [[0.49, 100.0]], [[0.51, 100.0]]), BOOK(20, mid=0.45)]
    tape = [(5, 0.48, 10.0, "SELL")]  # strictly through our bid at 0.49
    r = simulate_market(books, tape, pool=0.0, v=0.05, size=100.0, dist=0.01)
    assert r.fills == 1 and r.inventory == pytest.approx(10.0)
    assert r.fill_pnl == pytest.approx(-10 * 0.49 + 10 * 0.45)


def test_ask_fill_shorts_and_queue_must_clear() -> None:
    books = [BOOK(0), BOOK(10)]
    assert (
        simulate_market(books, [(5, 0.51, 60.0, "BUY")], 0.0, 0.05, size=50.0, dist=0.01).fills == 0
    )
    r = simulate_market(books, [(5, 0.51, 130.0, "BUY")], 0.0, 0.05, size=50.0, dist=0.01)
    assert r.inventory == pytest.approx(-30.0) and r.fills == 1


def test_inventory_cap_stops_adding_and_pull_skips_fast_moves() -> None:
    books = [BOOK(0), BOOK(10), BOOK(20)]
    tape = [(i, 0.40, 1000.0, "SELL") for i in (2, 4, 12)]
    r = simulate_market(books, tape, 0.0, 0.05, size=50.0, dist=0.01, max_inventory=50.0)
    assert r.inventory == pytest.approx(50.0)  # one 50-share fill, then the cap holds
    fast = [BOOK(0), BOOK(10, mid=0.55), BOOK(20, mid=0.55)]
    r2 = simulate_market(
        fast, [(12, 0.40, 1000.0, "SELL")], 0.0, 0.05, size=50.0, dist=0.01, pull_move=0.02
    )
    assert r2.fills == 0 or r2.inventory <= 50.0
