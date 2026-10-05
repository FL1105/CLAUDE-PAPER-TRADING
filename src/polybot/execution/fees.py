"""Fee model: the ONE place fee math lives.

Assumptions (UNVERIFIED; see docs/ASSUMPTIONS.md section 4):
    * Parabolic taker fee: fee_usdc = coeff * shares * p * (1 - p), charged per
      fill level at that level's price p. Maximal at p = 0.5, zero at 0 and 1.
    * ``coeff`` comes from config: per category, else the default. Categories
      listed as zero-fee, or ``formula: zero``, pay nothing.
    * Maker rebates are ignored (we only take liquidity).

Failure mode guarded against:
    * Cost drag being underestimated. The coefficient is configurable and every
      gate is also evaluated at ``stress_multiplier`` (2x) fees.
"""

from __future__ import annotations

from collections.abc import Iterable

from polybot.config import FeesConfig


def fee_coeff(cfg: FeesConfig, category: str | None, multiplier: float = 1.0) -> float:
    """Coefficient for a market category, times an optional stress multiplier."""
    if multiplier < 0:
        raise ValueError("multiplier must be >= 0")
    if cfg.formula == "zero":
        return 0.0
    if category is not None and category in cfg.zero_fee_categories:
        return 0.0
    base = cfg.per_category.get(category, cfg.default_coeff) if category else cfg.default_coeff
    return base * multiplier


def level_fee(shares: float, price: float, coeff: float) -> float:
    """Parabolic fee for ``shares`` filled at ``price``."""
    if shares < 0 or not 0.0 <= price <= 1.0 or coeff < 0:
        raise ValueError("need shares >= 0, 0 <= price <= 1, coeff >= 0")
    return coeff * shares * price * (1.0 - price)


def fill_fee(levels: Iterable[tuple[float, float]], coeff: float) -> float:
    """Total fee over (price, shares) fill levels."""
    return sum(level_fee(sh, px, coeff) for px, sh in levels)
