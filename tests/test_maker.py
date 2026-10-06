"""Conservative passive-maker replay: queue, through-trades, inventory, mark."""

from __future__ import annotations

import pytest

from polybot.maker import MakerParams, simulate_token

P = MakerParams(quote_usd=10.0, max_inventory_usd=50.0, min_spread=0.02)
BOOK = lambda ts: (ts, 0.50, 100.0, 0.54, 100.0)  # noqa: E731


def test_no_fill_while_queue_ahead_is_not_consumed() -> None:
    r = simulate_token([BOOK(0), BOOK(10)], [(5, 0.50, 60.0, "SELL")], P)
    assert r.fills_buy == 0 and r.pnl == 0.0


def test_fill_after_queue_and_pnl_marks_inventory() -> None:
    # 100 queue ahead, a 130-share sell at our bid: we get 30 shares capped by quote size (20)
    r = simulate_token([BOOK(0), BOOK(10)], [(5, 0.50, 130.0, "SELL")], P)
    assert r.fills_buy == 1 and r.inventory == pytest.approx(20.0)
    assert r.cash == pytest.approx(-10.0)
    assert r.pnl == pytest.approx(-10.0 + 20.0 * 0.52)  # marked at mid 0.52


def test_trade_through_fills_without_queue() -> None:
    r = simulate_token([BOOK(0), BOOK(10)], [(5, 0.48, 5.0, "SELL")], P)
    assert r.inventory == pytest.approx(5.0)


def test_round_trip_captures_spread_only_with_ask_fill() -> None:
    tape = [(2, 0.49, 20.0, "SELL"), (12, 0.55, 500.0, "BUY")]
    books = [BOOK(0), BOOK(10), (20, 0.50, 100.0, 0.54, 100.0)]
    r = simulate_token(books, tape, MakerParams(quote_usd=11.0, max_inventory_usd=50.0))
    assert r.fills_buy == 1 and r.fills_sell == 1 and r.inventory == pytest.approx(0.0)
    assert r.pnl == pytest.approx(20.0 * (0.54 - 0.50))


def test_inventory_cap_and_narrow_spread() -> None:
    tape = [(i, 0.40, 1000.0, "SELL") for i in (2, 3, 4, 5, 6, 7, 8)]
    r = simulate_token([BOOK(0), BOOK(10)], tape, P)
    assert r.inventory * 0.50 <= 50.0 + 1e-9
    narrow = [(0, 0.50, 100.0, 0.51, 100.0), (10, 0.50, 100.0, 0.51, 100.0)]
    assert simulate_token(narrow, [(5, 0.40, 9.0, "SELL")], P).fills_buy == 0


def test_liquid_tokens_skips_tight_and_inactive_markets() -> None:
    from polybot.maker import liquid_tokens

    def mk(cid: str, spread: float, vol: float) -> dict:
        return {
            "conditionId": cid,
            "question": "q?",
            "endDate": "2027-01-01T00:00:00Z",
            "closed": False,
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.5","0.5"]',
            "clobTokenIds": f'["{cid}Y","{cid}N"]',
            "spread": spread,
            "volume24hr": vol,
        }

    class Fake:
        def markets(self, **kw: object) -> list[dict]:
            return [mk("a", 0.001, 9e6), mk("b", 0.02, 1e5), mk("c", 0.04, 6e4), mk("d", 0.05, 100)]

    assert liquid_tokens(Fake(), 5) == [("c", "cY"), ("b", "bY")]  # ranked by volume x spread


def test_rebates_estimated_but_not_in_pnl() -> None:
    p = MakerParams(quote_usd=10.0, fee_rate=0.05, rebate_frac=0.2)
    r = simulate_token([BOOK(0), BOOK(10)], [(5, 0.48, 5.0, "SELL")], p)
    assert r.rebates == pytest.approx(0.2 * 0.05 * 5.0 * 0.50 * 0.50)
    assert r.pnl == pytest.approx(-5.0 * 0.50 + 5.0 * 0.52)
