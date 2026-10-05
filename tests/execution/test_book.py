from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from polybot.data.schemas import Level
from polybot.execution.book import depth_within, floor_to, snap_up, walk_book

L = Level


def test_single_level() -> None:
    r = walk_book([L(price=0.5, size=1000)], 50, tick=0.01)
    assert r.status == "filled" and r.shares == 100 and r.cost == pytest.approx(50)
    assert r.avg_price == 0.5 and r.best_ask == 0.5
    assert r.slippage_vs(0.48) == pytest.approx(0.02)


def test_multi_level_unsorted_input() -> None:
    asks = [L(price=0.52, size=100), L(price=0.50, size=40), L(price=0.51, size=50)]
    r = walk_book(asks, 60, tick=0.01)
    # 40 @ .50 = 20, 50 @ .51 = 25.5, remaining 14.5 / .52 = 27.88 shares
    assert r.levels == ((0.50, 40), (0.51, 50), (0.52, 27.88))
    assert r.status == "filled"
    assert r.cost == pytest.approx(20 + 25.5 + 27.88 * 0.52)
    assert r.avg_price == pytest.approx(r.cost / r.shares)
    assert r.avg_price > r.best_ask


def test_insufficient_depth_reject_and_partial() -> None:
    asks = [L(price=0.5, size=10)]
    rej = walk_book(asks, 50, tick=0.01)
    assert rej.status == "rejected" and rej.reason == "insufficient_depth" and rej.shares == 0
    part = walk_book(asks, 50, tick=0.01, insufficient="partial")
    assert part.status == "partial" and part.shares == 10 and part.cost == pytest.approx(5)


def test_tick_rounding_is_pessimistic() -> None:
    assert snap_up(0.4999999999, 0.01) == 0.5
    assert snap_up(0.501, 0.01) == 0.51
    assert snap_up(0.5, 0.001) == 0.5
    r = walk_book([L(price=0.503, size=1000)], 10, tick=0.01)
    assert r.avg_price == 0.51
    with pytest.raises(ValueError):
        snap_up(0.5, 0)
    assert floor_to(1.239999, 2) == 1.23


def test_empty_and_degenerate_books() -> None:
    assert walk_book([], 10, tick=0.01).reason == "empty_book"
    assert walk_book([L(price=0.5, size=0)], 10, tick=0.01).reason == "empty_book"
    assert walk_book([L(price=1.0, size=100)], 10, tick=0.01).reason == "empty_book"
    with pytest.raises(ValueError):
        walk_book([L(price=0.5, size=1)], 0, tick=0.01)


def test_max_price_chase_limit() -> None:
    asks = [L(price=0.50, size=20), L(price=0.60, size=1000)]
    r = walk_book(asks, 50, tick=0.01, max_price=0.55)
    assert r.status == "rejected" and r.reason == "insufficient_depth"
    r2 = walk_book(asks, 50, tick=0.01, max_price=0.55, insufficient="partial")
    assert r2.levels == ((0.5, 20),)
    r3 = walk_book([L(price=0.6, size=100)], 10, tick=0.01, max_price=0.55)
    assert r3.reason == "chase_limit"


def test_depth_within() -> None:
    asks = [L(price=0.50, size=100), L(price=0.53, size=100), L(price=0.60, size=100)]
    assert depth_within(asks, 0.50, 0.03) == pytest.approx(50 + 53)


levels = st.lists(
    st.tuples(st.integers(1, 98), st.floats(0.01, 5000, allow_nan=False)), min_size=1, max_size=8
)


@given(levels, st.floats(1.0, 2000.0))
@settings(max_examples=300, deadline=None)
def test_property_never_beats_best_ask_and_cost_matches(lv: list, stake: float) -> None:
    asks = [L(price=p / 100, size=s) for p, s in lv]
    r = walk_book(asks, stake, tick=0.01, insufficient="partial")
    assume(r.filled)
    assert r.avg_price is not None and r.best_ask is not None
    assert r.avg_price >= r.best_ask - 1e-12  # fill never beats the best ask
    assert r.best_ask == min(a.price for a in asks if a.size > 0)
    assert r.shares * r.avg_price == pytest.approx(r.cost, rel=1e-9)  # shares x price = cost
    assert r.cost <= stake + 1e-9  # never spend more than the stake
    if r.status == "filled":  # a complete fill spends the stake up to share rounding
        assert stake - r.cost <= max(p for p, _ in r.levels) * 0.01 * len(r.levels) + 1e-9
    for (p1, _), (p2, _) in zip(r.levels, r.levels[1:], strict=False):
        assert p1 < p2  # walked in price order
    for p, sh in r.levels:  # never takes more than the level offers
        assert sh <= sum(a.size for a in asks if abs(snap_up(a.price, 0.01) - p) < 1e-12) + 1e-9


@given(levels, st.floats(1.0, 2000.0))
@settings(max_examples=200, deadline=None)
def test_property_reject_mode_is_all_or_nothing(lv: list, stake: float) -> None:
    asks = [L(price=p / 100, size=s) for p, s in lv]
    r = walk_book(asks, stake, tick=0.01, insufficient="reject")
    assert r.status in ("filled", "rejected")
    if r.status == "rejected":
        assert r.shares == 0 and r.cost == 0


def test_levels_at_same_snapped_price_are_merged() -> None:
    r = walk_book(
        [L(price=0.501, size=10), L(price=0.509, size=10)], 100, tick=0.01, insufficient="partial"
    )
    assert r.levels == ((0.51, 20),)
