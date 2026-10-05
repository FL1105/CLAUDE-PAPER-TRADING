"""Walk the order book: average fill price, shares and slippage for a USDC stake.

Assumptions:
    * We BUY with a USDC budget by sweeping asks from the best (lowest) price up.
    * Level prices are snapped UP to the tick grid (pessimistic for a buyer),
      and shares are rounded DOWN to ``share_decimals`` (we never buy more than
      the budget allows).
    * ``max_price`` (from the chase rule) stops the sweep: levels above it are
      never taken.
    * Insufficient depth: ``reject`` returns no fill; ``partial`` returns what was
      available.

Failure mode guarded against:
    * Unfillable signals: a backtest that fills at the whale's price assumes
      liquidity that was not there for us. Every paper fill here pays the
      visible asks, never better than the best ask.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from polybot.data.schemas import Level

EPS = 1e-9
FillStatus = Literal["filled", "partial", "rejected"]


@dataclass(frozen=True)
class FillResult:
    status: FillStatus
    reason: str
    shares: float
    cost: float  # USDC spent (excluding fees)
    avg_price: float | None
    best_ask: float | None
    levels: tuple[tuple[float, float], ...] = field(default_factory=tuple)  # (price, shares)

    @property
    def filled(self) -> bool:
        return self.status != "rejected" and self.shares > 0

    def slippage_vs(self, ref: float | None) -> float | None:
        """avg fill price minus a reference price (positive = we paid more)."""
        if self.avg_price is None or ref is None:
            return None
        return self.avg_price - ref


def snap_up(price: float, tick: float) -> float:
    """Round a price UP to the tick grid (float-noise tolerant)."""
    if tick <= 0:
        raise ValueError("tick must be > 0")
    return round(math.ceil(price / tick - EPS) * tick, 10)


def floor_to(x: float, decimals: int) -> float:
    f = 10.0**decimals
    return math.floor(x * f + EPS) / f


def walk_book(
    asks: Sequence[Level],
    usdc_stake: float,
    *,
    tick: float,
    insufficient: Literal["reject", "partial"] = "reject",
    max_price: float | None = None,
    share_decimals: int = 2,
) -> FillResult:
    """Spend up to ``usdc_stake`` sweeping ``asks`` (any order; sorted here)."""
    if usdc_stake <= 0:
        raise ValueError("usdc_stake must be > 0")
    merged: dict[float, float] = {}
    for lv in asks:  # merge levels that share a (tick-snapped) price
        if lv.size > 0:
            p = snap_up(lv.price, tick)
            merged[p] = merged.get(p, 0.0) + lv.size
    book = sorted(merged.items())
    book = [(p, s) for p, s in book if 0.0 < p < 1.0]
    if not book:
        return FillResult("rejected", "empty_book", 0.0, 0.0, None, None)
    best = book[0][0]
    lot = 10.0**-share_decimals
    remaining = usdc_stake
    taken: list[tuple[float, float]] = []
    stop = "book_exhausted"
    for price, size in book:
        if max_price is not None and price > max_price + EPS:
            stop = "chase_limit"
            break
        if remaining < price * lot - EPS:  # cannot afford even one lot here: budget used up
            stop = "budget"
            break
        budget_bound = remaining / price < size
        shares = floor_to(min(size, remaining / price), share_decimals)
        if shares > 0:
            taken.append((price, shares))
            remaining -= shares * price
        if budget_bound:
            stop = "budget"
            break
    if not taken:
        reason = "chase_limit" if stop == "chase_limit" else "no_depth"
        return FillResult("rejected", reason, 0.0, 0.0, None, best)
    shares_total = sum(s for _, s in taken)
    cost = sum(p * s for p, s in taken)
    complete = stop == "budget" or remaining < taken[-1][0] * lot + EPS
    if not complete and insufficient == "reject":
        return FillResult("rejected", "insufficient_depth", 0.0, 0.0, None, best)
    status: FillStatus = "filled" if complete else "partial"
    return FillResult(
        status,
        "" if complete else "insufficient_depth",
        shares_total,
        cost,
        cost / shares_total,
        best,
        tuple(taken),
    )


def depth_within(asks: Sequence[Level], best_ask: float, band: float) -> float:
    """USDC notional of asks priced within ``band`` of the best ask."""
    return sum(
        lv.price * lv.size for lv in asks if lv.size > 0 and lv.price <= best_ask + band + EPS
    )
