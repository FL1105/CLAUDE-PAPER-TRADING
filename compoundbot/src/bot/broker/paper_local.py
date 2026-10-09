"""Local paper broker. Uses the SAME cost model as the backtest (per-side bps + spread/slippage,
fixed fees), fills at the supplied quote, supports fractional shares, idempotent order ids."""
from __future__ import annotations

from bot.broker.base import Account, Broker, Fill, Order
from bot.config import Costs, Universe


class PaperLocalBroker(Broker):
    mode = "paper"

    def __init__(self, costs: Costs, universe: Universe, cash: float | None = None,
                 fractional: bool = True, execution: str = "next_open"):
        """execution="next_open": orders are queued and fill at the next session's open (call
        `settle(open_prices)`), matching the backtest timing. "immediate": fill at the current quote."""
        if execution not in ("next_open", "immediate"):
            raise ValueError("execution must be next_open or immediate")
        self.execution = execution
        self.pending: list[Order] = []
        self.costs, self.universe = costs, universe
        self.cash = float(cash if cash is not None else costs.start_capital_usd)
        self.positions: dict[str, float] = {}
        self.quotes: dict[str, float] = {}
        self.fills: dict[str, Fill] = {}
        self.fractional = fractional
        self.cost_paid = 0.0

    def dump(self) -> dict:
        return {"cash": self.cash, "positions": self.get_positions(), "cost_paid": self.cost_paid,
                "pending": [vars(o) for o in self.pending]}

    def load(self, d: dict) -> None:
        self.cash, self.positions = float(d["cash"]), dict(d["positions"])
        self.cost_paid = float(d.get("cost_paid", 0.0))
        self.pending = [Order(**o) for o in d.get("pending", [])]

    # market data is pushed in by the runner (closing / opening prices)
    def set_quotes(self, quotes: dict[str, float]) -> None:
        self.quotes.update(quotes)

    def get_quote(self, symbol: str) -> float:
        return self.quotes[symbol]

    def get_positions(self) -> dict[str, float]:
        return {s: q for s, q in self.positions.items() if abs(q) > 1e-12}

    def get_account(self) -> Account:
        eq = self.cash + sum(q * self.quotes.get(s, 0.0) for s, q in self.positions.items())
        return Account(equity=eq, cash=self.cash, positions=self.get_positions())

    def submit_order(self, order: Order) -> Fill:
        if order.client_order_id in self.fills:  # idempotent
            return self.fills[order.client_order_id]
        for q in self.pending:
            if q.client_order_id == order.client_order_id:
                return Fill(q.client_order_id, q.symbol, q.side, 0.0, 0.0, status="open")
        if self.execution == "next_open":
            self.pending.append(order)
            return Fill(order.client_order_id, order.symbol, order.side, 0.0, 0.0, status="open")
        return self._execute(order, self.quotes.get(order.symbol))

    def settle(self, open_prices: dict[str, float]) -> list[Fill]:
        """Fill queued orders at the session open (sells first, as the order planner sorts them)."""
        queue, self.pending = self.pending, []
        out = []
        for o in queue:
            f = self._execute(o, open_prices.get(o.symbol))
            out.append(f)
        return out

    def _execute(self, order: Order, px: float | None) -> Fill:
        if px is None or px <= 0:
            return self._reject(order, "no price")
        qty = order.qty if self.fractional else float(int(order.qty))
        if qty <= 0:
            return self._reject(order, "qty rounds to zero")
        cls = self.universe.cls_of(order.symbol)
        slip = self.costs.side_bps(cls) / 1e4
        fill_px = px * (1 + slip) if order.side == "buy" else px * (1 - slip)
        if order.limit_price is not None:
            if (order.side == "buy" and fill_px > order.limit_price) or \
               (order.side == "sell" and fill_px < order.limit_price):
                return self._reject(order, "limit not marketable")
        fee = self.costs.commission_usd_per_order + self.costs.fixed_fee_usd_per_order
        gross = qty * fill_px
        if order.side == "buy":
            if gross + fee > self.cash + 1e-9:
                return self._reject(order, "insufficient cash")
            self.cash -= gross + fee
            self.positions[order.symbol] = self.positions.get(order.symbol, 0.0) + qty
        else:
            have = self.positions.get(order.symbol, 0.0)
            if qty > have + 1e-9:
                return self._reject(order, "would short")
            self.cash += gross - fee
            self.positions[order.symbol] = have - qty
        self.cost_paid += abs(qty * (fill_px - px)) + fee
        f = Fill(order.client_order_id, order.symbol, order.side, qty, fill_px,
                 cost_usd=abs(qty * (fill_px - px)) + fee, ref_price=px, fee_usd=fee)
        self.fills[order.client_order_id] = f
        return f

    def _reject(self, order: Order, reason: str) -> Fill:
        f = Fill(order.client_order_id, order.symbol, order.side, 0.0, 0.0, status="rejected",
                 reason=reason)
        self.fills[order.client_order_id] = f
        return f

    def cancel(self, client_order_id: str) -> bool:
        n = len(self.pending)
        self.pending = [q for q in self.pending if q.client_order_id != client_order_id]
        return len(self.pending) < n

    def get_order(self, client_order_id: str) -> Fill | None:
        if client_order_id in self.fills:
            return self.fills[client_order_id]
        for q in self.pending:
            if q.client_order_id == client_order_id:
                return Fill(q.client_order_id, q.symbol, q.side, 0.0, 0.0, status="open")
        return None
