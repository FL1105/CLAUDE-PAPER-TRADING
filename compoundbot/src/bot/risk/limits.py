"""Pure risk functions. Limits come from config/risk.yaml; nothing here is model-editable."""
from __future__ import annotations

import math
from dataclasses import dataclass

from bot.config import Risk


@dataclass(frozen=True)
class OrderCheck:
    ok: bool
    reason: str = ""


def clip_weights(weights: dict[str, float], classes: dict[str, str], risk: Risk) -> tuple[dict[str, float], list[str]]:
    """Enforce: no shorts, per-asset cap, crypto sleeve cap, gross cap. NaN/inf become 0.
    Excess is left as cash (never redistributed). Returns (weights, notes)."""
    notes: list[str] = []
    w: dict[str, float] = {}
    for s, x in weights.items():
        if not math.isfinite(x):
            notes.append(f"{s}: non-finite weight set to 0")
            x = 0.0
        if x < 0:
            notes.append(f"{s}: negative weight set to 0 (no shorts)")
            x = 0.0
        if x > risk.max_weight_per_asset:
            notes.append(f"{s}: capped {x:.3f} -> {risk.max_weight_per_asset}")
            x = risk.max_weight_per_asset
        w[s] = x
    crypto = [s for s in w if classes.get(s) == "crypto"]
    tot_c = sum(w[s] for s in crypto)
    if tot_c > risk.crypto_sleeve_max + 1e-12:
        f = risk.crypto_sleeve_max / tot_c
        for s in crypto:
            w[s] *= f
        notes.append(f"crypto sleeve scaled by {f:.3f}")
    gross = sum(w.values())
    if gross > risk.gross_exposure_max + 1e-12:
        f = risk.gross_exposure_max / gross
        w = {s: x * f for s, x in w.items()}
        notes.append(f"gross scaled by {f:.3f}")
    return w, notes


def check_order(
    symbol: str, side: str, qty: float, price: float, limit_price: float | None, equity: float,
    adv_usd: float | None, risk: Risk, current_value: float = 0.0,
) -> OrderCheck:
    """Reject orders that are non-finite, too large, too big vs ADV, or priced away from the quote."""
    for name, v in (("qty", qty), ("price", price), ("equity", equity)):
        if not math.isfinite(v) or v <= 0:
            return OrderCheck(False, f"invalid {name}: {v}")
    notional = qty * price
    cap = (risk.max_weight_per_asset + risk.order_cap_over_max_weight) * equity
    if notional > cap + 1e-9:
        return OrderCheck(False, f"order {notional:.0f} exceeds cap {cap:.0f}")
    if side == "sell" and qty * price > current_value * 1.0001 + 1e-9:
        return OrderCheck(False, "sell exceeds current position (no shorts)")
    if adv_usd is not None and math.isfinite(adv_usd) and adv_usd > 0:
        if notional > risk.order_cap_adv_frac * adv_usd:
            return OrderCheck(False, f"order is {notional / adv_usd:.2%} of ADV")
    if limit_price is not None:
        if not math.isfinite(limit_price) or limit_price <= 0:
            return OrderCheck(False, "invalid limit price")
        if abs(limit_price / price - 1) > risk.price_collar + 1e-12:
            return OrderCheck(False, "limit price outside collar")
    return OrderCheck(True)


def daily_loss_exceeded(start_equity: float, equity: float, risk: Risk) -> bool:
    if not (math.isfinite(start_equity) and math.isfinite(equity)) or start_equity <= 0:
        return True  # fail safe
    return (start_equity - equity) / start_equity >= risk.daily_loss_halt


def data_healthy(latest_dates: dict[str, object], required: list[str], today, max_lag_sessions: int,
                 session_lag) -> tuple[bool, list[str]]:
    """latest_dates: symbol -> last bar date. session_lag(date, today) -> number of sessions behind."""
    bad = []
    for s in required:
        d = latest_dates.get(s)
        if d is None or session_lag(d, today) > max_lag_sessions:
            bad.append(s)
    return (not bad), bad
