"""Latency models: where the price is by the time WE can trade.

Three sources of a fill reference, in order of trust:
    1. live_forward: a real order book fetched at detection (and shadow books at
       1x/2x/5x L). No modeling; the paper loop records them (Phase 6).
    2. tape (real-data backtest): the last real trade price in the same token at
       or before t + delay, after the wallet's own trade. Real, but it is a trade
       price, not a book, so a synthetic spread is added on top.
    3. drift (synthetic worlds): p(d) = p + (q - p) * (1 - 2^(-d / h)): after an
       informed buy, price moves toward the informed value q with half-life h.

Assumption: the synthetic book (``synthetic_asks``) is a stand-in with
configurable half-spread and depth. It must be replaced by spreads and depth
measured from real snapshots before backtest costs are trusted
(docs/ASSUMPTIONS.md).

Failure mode guarded against: latency decay. By the time we see a wallet's trade,
the edge may be priced in. Every backtest fill is taken at a delayed price, never
at the wallet's own price.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from polybot.data.schemas import Level

PRICE_MIN, PRICE_MAX = 0.001, 0.999


def drift_price(p_wallet: float, q_informed: float, delay_s: float, half_life_s: float) -> float:
    """Price after ``delay_s`` seconds of exponential drift toward ``q_informed``."""
    if delay_s < 0 or half_life_s <= 0:
        raise ValueError("delay_s must be >= 0 and half_life_s > 0")
    frac = 1.0 - float(2.0 ** (-delay_s / half_life_s))
    return p_wallet + (q_informed - p_wallet) * frac


def synthetic_asks(
    ref_price: float, *, half_spread: float, levels: int, depth_usdc: float, tick: float
) -> list[Level]:
    """A ladder of asks starting at ref + half_spread, one tick apart, ``depth_usdc`` each."""
    if levels < 1 or depth_usdc <= 0 or tick <= 0 or half_spread < 0:
        raise ValueError("bad synthetic book parameters")
    out: list[Level] = []
    first = ref_price + half_spread
    for i in range(levels):
        p = round(first + i * tick, 10)
        if p >= PRICE_MAX:
            break
        p = max(p, PRICE_MIN)
        out.append(Level(price=p, size=depth_usdc / p))
    return out


def tape_price(
    ts: npt.NDArray[np.int64], prices: npt.NDArray[np.float64], at_ts: int, after_ts: int
) -> float | None:
    """Last tape price with after_ts <= ts <= at_ts (arrays sorted by ts); None if none.

    ``after_ts`` should be the wallet's own trade time, so we never use a price
    from before the signal existed.
    """
    if ts.shape != prices.shape:
        raise ValueError("ts and prices must align")
    hi = int(np.searchsorted(ts, at_ts, side="right"))
    lo = int(np.searchsorted(ts, after_ts, side="left"))
    if hi <= lo:
        return None
    return float(prices[hi - 1])
