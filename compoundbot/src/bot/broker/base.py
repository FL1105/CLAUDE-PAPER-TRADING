"""Broker interface and order types. Paper and local-simulator brokers implement it.
There is deliberately no real-money implementation (see live_stub.py and test_safety.py)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class NotEnabled(RuntimeError):
    """Raised by anything that would touch real money."""


@dataclass(frozen=True)
class Order:
    client_order_id: str
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    limit_price: float | None = None
    notional_hint: float = 0.0  # qty * reference price, for risk checks and logs

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if not self.qty > 0:
            raise ValueError("qty must be > 0")


@dataclass
class Fill:
    client_order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    cost_usd: float = 0.0
    status: str = "filled"  # filled | partial | rejected | open
    reason: str = ""
    ref_price: float = 0.0  # price we compare the fill against (session open / quote); 0 = unknown
    fee_usd: float = 0.0  # explicit fee reported by the broker, if any (0 = none or unknown)


@dataclass
class Account:
    equity: float
    cash: float
    positions: dict[str, float] = field(default_factory=dict)  # symbol -> qty


class Broker(ABC):
    mode = "paper"

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> dict[str, float]: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> float: ...

    @abstractmethod
    def submit_order(self, order: Order) -> Fill: ...

    @abstractmethod
    def cancel(self, client_order_id: str) -> bool: ...

    @abstractmethod
    def get_order(self, client_order_id: str) -> Fill | None: ...
