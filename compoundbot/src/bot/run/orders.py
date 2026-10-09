"""Turn target weights into orders: diff vs positions, no-trade band, min order, rounding,
sells before buys, marketable limits inside the price collar, idempotent client ids, risk checks."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.broker.base import Broker, Fill, Order
from bot.config import Costs, Risk, Universe
from bot.risk.killswitch import KillSwitch
from bot.risk.limits import check_order, clip_weights

CASH_BUFFER = 0.002  # keep a sliver of equity in cash so slippage cannot cause rejects


@dataclass
class PlannedOrder:
    order: Order
    reason: str = ""


@dataclass
class ExecReport:
    planned: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _round_qty(qty: float, fractional: bool, selling_all: bool) -> float:
    if fractional:
        return math.floor(qty * 1e4) / 1e4 if not selling_all else qty
    return float(math.floor(qty))


def plan_orders(
    targets: dict[str, float], positions: dict[str, float], quotes: dict[str, float],
    equity: float, risk: Risk, costs: Costs, universe: Universe, run_id: str,
    fractional: bool = True, adv_usd: dict[str, float] | None = None,
) -> tuple[list[Order], list[str]]:
    classes = {s: universe.cls_of(s) for s in set(targets) | set(positions) if s in universe.symbols}
    w, notes = clip_weights({s: targets.get(s, 0.0) for s in classes}, classes, risk)
    orders: list[Order] = []
    for s in sorted(classes):
        px = quotes.get(s)
        if px is None or not math.isfinite(px) or px <= 0:
            notes.append(f"{s}: no valid quote, skipped")
            continue
        cur_q = positions.get(s, 0.0)
        cur_v = cur_q * px
        tgt_v = w.get(s, 0.0) * equity * (1 - CASH_BUFFER)
        delta_v = tgt_v - cur_v
        exit_all = w.get(s, 0.0) == 0 and cur_q > 0
        if not exit_all:
            if abs(delta_v) / equity <= risk.no_trade_band:
                continue
            if abs(delta_v) < costs.min_order_usd:
                continue
        if delta_v > 0:
            side, qty = "buy", _round_qty(delta_v / px, fractional, False)
            limit = px * (1 + 0.9 * risk.price_collar)
        elif delta_v < 0:
            side = "sell"
            qty = _round_qty(min(-delta_v / px, cur_q) if not exit_all else cur_q, fractional, exit_all)
            limit = px * (1 - 0.9 * risk.price_collar)
        else:
            continue
        if qty <= 0:
            continue
        chk = check_order(s, side, qty, px, limit, equity, (adv_usd or {}).get(s), risk, current_value=cur_v)
        if not chk.ok:
            notes.append(f"{s} {side} blocked by risk: {chk.reason}")
            continue
        orders.append(Order(f"{run_id}-{s.replace('/', '')}-{side}", s, side, qty, limit, qty * px))
    orders.sort(key=lambda o: (o.side != "sell", o.symbol))  # sell first
    return orders, notes


def execute(orders: list[Order], broker: Broker, kill: KillSwitch) -> ExecReport:
    rep = ExecReport(planned=list(orders))
    for o in orders:
        kill.assert_clear()  # checked before EVERY order
        f = broker.submit_order(o)
        rep.fills.append(f)
        if f.status == "rejected":
            rep.notes.append(f"{o.symbol} {o.side} rejected: {f.reason}")
    return rep
