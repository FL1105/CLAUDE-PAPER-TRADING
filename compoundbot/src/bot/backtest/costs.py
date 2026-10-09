"""Cost model: per-side bps (+spread/slippage) on traded notional, plus fixed fees and minimum order."""
from __future__ import annotations

import numpy as np

from bot.config import Costs


def side_bps_vector(costs: Costs, classes: list[str], multiplier: float = 1.0) -> np.ndarray:
    return np.array([costs.side_bps(c, multiplier) for c in classes]) / 1e4


def trade_cost_fraction(
    deltas: np.ndarray, side_rate: np.ndarray, equity_usd: float, costs: Costs,
    multiplier: float = 1.0
) -> tuple[float, int]:
    """Cost as a fraction of equity for the given weight changes, and the number of orders.

    side_rate already includes the cost multiplier; fixed fees are scaled here."""
    traded = np.abs(deltas)
    n_orders = int((traded > 1e-12).sum())
    proportional = float((traded * side_rate).sum())
    fixed = (costs.commission_usd_per_order + costs.fixed_fee_usd_per_order) * multiplier * n_orders
    return proportional + fixed / equity_usd, n_orders


def round_trip_bps(costs: Costs, cls: str, multiplier: float = 1.0) -> float:
    return 2 * costs.side_bps(cls, multiplier)
