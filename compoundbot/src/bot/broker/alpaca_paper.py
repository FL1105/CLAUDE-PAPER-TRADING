"""Alpaca PAPER trading adapter (REST via httpx).

UNVERIFIED: written without network access. Endpoint paths, field names and enums below follow
the author's recollection of Alpaca's Trading API v2 and MUST be checked against the current
official docs (log URL + date in docs/SOURCES.md) before the first real paper run.

Safety: the trading base URL is pinned to the paper host and asserted at construction.
"""
from __future__ import annotations

import math
import os

import httpx

from bot.broker.base import Account, Broker, Fill, Order

PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"


class NotPaperEndpoint(RuntimeError):
    pass


def _alpaca_symbol(symbol: str) -> str:
    return symbol  # crypto uses "BTC/USD" form in order requests (UNVERIFIED)


def tick_round(price: float, side: str) -> float:
    """Round a limit price to a valid tick in the SAFE direction (buys up, sells down, so the order stays
    at least as marketable). Stocks at or above $1 trade in whole cents; below that, 4 decimals."""
    step = 0.01 if price >= 1.0 else 0.0001
    n = price / step
    n = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    return round(n * step, 4)


def floor_qty(qty: float, places: int = 6) -> float:
    """Never round a quantity UP (a sell above the held amount is rejected)."""
    f = 10 ** places
    return math.floor(qty * f + 1e-9) / f


class AlpacaPaperBroker(Broker):
    mode = "paper"
    UNVERIFIED = True

    def __init__(self, key_id: str | None = None, secret: str | None = None,
                 base_url: str = PAPER_BASE, client: httpx.Client | None = None,
                 fractional: bool = True, crypto_symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD")):
        if base_url.rstrip("/") != PAPER_BASE:
            raise NotPaperEndpoint(f"refusing non-paper endpoint {base_url}")
        key_id = key_id or os.environ.get("ALPACA_PAPER_KEY_ID", "")
        secret = secret or os.environ.get("ALPACA_PAPER_SECRET", "")
        if client is None and not (key_id and secret):
            raise RuntimeError("ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET not set (see .env.example)")
        self.base = PAPER_BASE
        self.client = client or httpx.Client(
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}, timeout=30)
        self.fractional = fractional
        # Alpaca reports crypto positions as "BTCUSD" while we use "BTC/USD" everywhere else
        self._from_alpaca = {c.replace("/", ""): c for c in crypto_symbols}

    def _ours(self, symbol: str) -> str:
        return self._from_alpaca.get(symbol, symbol)

    # ------------------------------------------------------------------ account
    def get_account(self) -> Account:
        r = self.client.get(f"{self.base}/v2/account")
        r.raise_for_status()
        a = r.json()
        return Account(equity=float(a["equity"]), cash=float(a["cash"]),
                       positions=self.get_positions())

    def get_positions(self) -> dict[str, float]:
        r = self.client.get(f"{self.base}/v2/positions")
        r.raise_for_status()
        return {self._ours(p["symbol"]): float(p["qty"]) for p in r.json()}

    def get_quote(self, symbol: str) -> float:
        sym = _alpaca_symbol(symbol)
        if "/" in sym:
            r = self.client.get(f"{DATA_BASE}/v1beta3/crypto/us/latest/trades", params={"symbols": sym})
            r.raise_for_status()
            return float(r.json()["trades"][sym]["p"])
        r = self.client.get(f"{DATA_BASE}/v2/stocks/{sym}/trades/latest")
        r.raise_for_status()
        return float(r.json()["trade"]["p"])

    # ------------------------------------------------------------------ orders
    def submit_order(self, order: Order) -> Fill:
        existing = self.get_order(order.client_order_id)
        if existing is not None:  # idempotent: never double-submit
            return existing
        qty = floor_qty(order.qty)
        if qty <= 0:
            return Fill(order.client_order_id, order.symbol, order.side, 0.0, 0.0, status="rejected",
                        reason="quantity rounds to zero")
        body = {
            "symbol": _alpaca_symbol(order.symbol), "qty": f"{qty:.6f}".rstrip("0").rstrip("."),
            "side": order.side, "client_order_id": order.client_order_id,
            "type": "limit" if order.limit_price else "market",
            "time_in_force": "gtc" if "/" in order.symbol else "day",
        }
        if order.limit_price:
            body["limit_price"] = f"{tick_round(order.limit_price, order.side):.4f}".rstrip("0").rstrip(".")
        r = self.client.post(f"{self.base}/v2/orders", json=body)
        if r.status_code >= 400:
            return Fill(order.client_order_id, order.symbol, order.side, 0.0, 0.0,
                        status="rejected", reason=f"HTTP {r.status_code}: {r.text[:200]}")
        return self._to_fill(r.json())

    def get_order(self, client_order_id: str) -> Fill | None:
        r = self.client.get(f"{self.base}/v2/orders:by_client_order_id",
                            params={"client_order_id": client_order_id})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return self._to_fill(r.json())

    def cancel(self, client_order_id: str) -> bool:
        o = self.client.get(f"{self.base}/v2/orders:by_client_order_id",
                            params={"client_order_id": client_order_id})
        if o.status_code != 200:
            return False
        r = self.client.delete(f"{self.base}/v2/orders/{o.json()['id']}")
        return r.status_code in (200, 204)

    @staticmethod
    def _to_fill(o: dict) -> Fill:
        st = o.get("status", "")
        status = {"filled": "filled", "partially_filled": "partial", "rejected": "rejected",
                  "canceled": "rejected", "expired": "rejected"}.get(st, "open")
        if status == "rejected" and float(o.get("filled_qty") or 0) > 0:
            status = "partial"  # cancelled/expired after a partial fill: the filled part is real
        sym = o["symbol"].replace("USD", "/USD") if ("/" not in o["symbol"] and o["symbol"].endswith("USD")
                                                      and len(o["symbol"]) > 5) else o["symbol"]
        return Fill(o["client_order_id"], sym, o["side"], float(o.get("filled_qty") or 0),
                    float(o.get("filled_avg_price") or 0), status=status,
                    reason=o.get("reject_reason", "") or st)
