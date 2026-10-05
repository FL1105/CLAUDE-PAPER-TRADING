"""Adapter layer: raw Polymarket JSON -> validated internal records.

Assumptions (ALL UNVERIFIED until ``polybot probe`` runs against the live API;
see docs/ASSUMPTIONS.md section "Assumed schemas"):
    * Gamma ``/markets`` items carry ``conditionId``, ``question``, ``endDate``
      (ISO-8601), ``closed``, and JSON-*encoded strings* ``outcomes``,
      ``outcomePrices`` and ``clobTokenIds``. We accept real lists too.
    * Data ``/trades`` items carry ``proxyWallet``, ``side`` (BUY/SELL), ``asset``
      (token id), ``conditionId``, ``size`` (shares), ``price``, ``timestamp``
      (epoch seconds), ``outcomeIndex``, ``transactionHash``.
    * CLOB ``/book`` returns ``asset_id``, ``timestamp`` (epoch ms, as string),
      ``bids``/``asks`` as ``[{"price": "0.48", "size": "120.5"}]`` with string numbers
      in an order we do NOT rely on (we sort explicitly).

Failure modes guarded against:
    * Fabricated fields: every field we read is declared here; a missing required
      field raises :class:`SchemaDriftError` (counted, surfaced in the report)
      instead of defaulting silently. Unknown extra fields are tolerated.
    * Inferring winners from last trade price: resolution comes ONLY from the
      ``closed`` flag plus final settlement ``outcomePrices`` being one-hot
      (resolved) or all-equal (void / 50-50). Anything else is ``ambiguous`` and
      excluded, never guessed.
    * Book order quirks: asks are sorted ascending, bids descending, here, once.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class SchemaDriftError(ValueError):
    """Raw payload did not match the schema we code against."""


DRIFT_COUNTER: Counter[str] = Counter()  # endpoint -> number of drift errors seen


def _drift(endpoint: str, exc: Exception) -> SchemaDriftError:
    DRIFT_COUNTER[endpoint] += 1
    return SchemaDriftError(f"{endpoint}: {exc}")


# --- helpers ------------------------------------------------------------------------


def parse_ts(value: Any) -> int | None:
    """Parse epoch seconds / epoch ms / ISO-8601 into UTC epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"bad timestamp {value!r}")
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)  # ms vs s heuristic (1e11 s = year 5138)
    s = str(value).strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", s):  # "+00" -> "+00:00"
        s += ":00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _json_list(value: Any) -> list[Any]:
    """Gamma returns some list fields as JSON-encoded strings; accept both forms."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value) if value.strip() else []
        if not isinstance(parsed, list):
            raise ValueError(f"expected JSON list, got {type(parsed).__name__}")
        return parsed
    raise ValueError(f"expected list or JSON string, got {type(value).__name__}")


# --- internal records ---------------------------------------------------------------


class ResolutionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    VOID = "void"  # 50-50 / invalid: excluded from edge stats, settled at refund value
    AMBIGUOUS = "ambiguous"  # closed but prices not clean: excluded, counted, never guessed


class _Rec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MarketRecord(_Rec):
    condition_id: str
    question: str
    end_ts: int | None
    resolved_ts: int | None
    resolution_status: ResolutionStatus
    winning_outcome_index: int | None
    settlement_prices: tuple[float, ...]
    category: str | None
    neg_risk: bool
    fee_coeff_at_trade_time: float | None = None


class TokenRecord(_Rec):
    token_id: str
    condition_id: str
    outcome_index: int
    outcome_label: str


class TradeRecord(_Rec):
    trade_key: str
    tx_hash: str | None
    ts: int
    wallet: str
    condition_id: str
    token_id: str
    side: str
    price: float
    size_shares: float
    size_usdc: float
    outcome_index: int | None


class Level(_Rec):
    price: float
    size: float


class BookSnapshot(_Rec):
    token_id: str
    ts: int
    bids: tuple[Level, ...]  # best (highest) first
    asks: tuple[Level, ...]  # best (lowest) first
    tick_size: float | None
    min_order_size: float | None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None


# --- raw models (what we ASSUME the API sends) -------------------------------------------


class _Raw(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class RawGammaMarket(_Raw):
    condition_id: str = Field(alias="conditionId", min_length=1)
    question: str
    end_date: Any = Field(default=None, alias="endDate")
    closed_time: Any = Field(default=None, alias="closedTime")
    closed: bool
    outcomes: Any
    outcome_prices: Any = Field(default=None, alias="outcomePrices")
    clob_token_ids: Any = Field(alias="clobTokenIds")
    neg_risk: bool | None = Field(default=None, alias="negRisk")
    category: str | None = None


class RawTrade(_Raw):
    proxy_wallet: str = Field(alias="proxyWallet", min_length=1)
    side: str
    asset: str = Field(min_length=1)
    condition_id: str = Field(alias="conditionId", min_length=1)
    size: float
    price: float
    timestamp: Any
    outcome_index: int | None = Field(default=None, alias="outcomeIndex")
    transaction_hash: str | None = Field(default=None, alias="transactionHash")

    @field_validator("side")
    @classmethod
    def _side(cls, v: str) -> str:
        up = v.upper()
        if up not in {"BUY", "SELL"}:
            raise ValueError(f"unknown side {v!r}")
        return up


class RawLevel(_Raw):
    price: float
    size: float


class RawBook(_Raw):
    asset_id: str = Field(min_length=1)
    timestamp: Any
    bids: list[RawLevel]
    asks: list[RawLevel]
    tick_size: float | None = None
    min_order_size: float | None = None


# --- adapters -----------------------------------------------------------------------


def _resolution(closed: bool, prices: list[float]) -> tuple[ResolutionStatus, int | None]:
    if not closed:
        return ResolutionStatus.OPEN, None
    if len(prices) >= 2:
        ones = [i for i, p in enumerate(prices) if p == 1.0]
        zeros = [p for p in prices if p == 0.0]
        if len(ones) == 1 and len(zeros) == len(prices) - 1:
            return ResolutionStatus.RESOLVED, ones[0]
        if all(abs(p - prices[0]) < 1e-12 for p in prices) and abs(sum(prices) - 1.0) < 1e-9:
            return ResolutionStatus.VOID, None
    return ResolutionStatus.AMBIGUOUS, None


def parse_gamma_market(raw: dict[str, Any]) -> tuple[MarketRecord, list[TokenRecord]]:
    try:
        m = RawGammaMarket.model_validate(raw)
        labels = [str(x) for x in _json_list(m.outcomes)]
        token_ids = [str(x) for x in _json_list(m.clob_token_ids)]
        prices = [float(x) for x in _json_list(m.outcome_prices)]
        if token_ids and len(labels) != len(token_ids):
            raise ValueError(f"{len(labels)} outcomes vs {len(token_ids)} token ids")
        status, winner = _resolution(m.closed, prices)
        end_ts = parse_ts(m.end_date)
        resolved_ts = parse_ts(m.closed_time) if status != ResolutionStatus.OPEN else None
    except (ValidationError, ValueError, TypeError) as exc:
        raise _drift("gamma/markets", exc) from exc
    market = MarketRecord(
        condition_id=m.condition_id,
        question=m.question,
        end_ts=end_ts,
        resolved_ts=resolved_ts,
        resolution_status=status,
        winning_outcome_index=winner,
        settlement_prices=tuple(prices) if status != ResolutionStatus.OPEN else (),
        category=m.category,
        neg_risk=bool(m.neg_risk),
    )
    tokens = [
        TokenRecord(token_id=t, condition_id=m.condition_id, outcome_index=i, outcome_label=lbl)
        for i, (t, lbl) in enumerate(zip(token_ids, labels, strict=True))
    ]
    return market, tokens


def trade_key(t: RawTrade, ts: int) -> str:
    """A tx hash alone is NOT unique (one tx can fill several orders), so hash the tuple."""
    parts = [
        t.transaction_hash or "",
        t.proxy_wallet.lower(),
        t.asset,
        t.side,
        repr(t.size),
        repr(t.price),
        str(ts),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def parse_trade(raw: dict[str, Any]) -> TradeRecord:
    try:
        t = RawTrade.model_validate(raw)
        ts = parse_ts(t.timestamp)
        if ts is None:
            raise ValueError("missing timestamp")
        if not (0.0 <= t.price <= 1.0) or t.size < 0:
            raise ValueError(f"out-of-range price/size {t.price}/{t.size}")
    except (ValidationError, ValueError, TypeError) as exc:
        raise _drift("data/trades", exc) from exc
    return TradeRecord(
        trade_key=trade_key(t, ts),
        tx_hash=t.transaction_hash,
        ts=ts,
        wallet=t.proxy_wallet.lower(),
        condition_id=t.condition_id,
        token_id=t.asset,
        side=t.side,
        price=t.price,
        size_shares=t.size,
        size_usdc=t.size * t.price,
        outcome_index=t.outcome_index,
    )


def parse_book(raw: dict[str, Any]) -> BookSnapshot:
    try:
        b = RawBook.model_validate(raw)
        ts = parse_ts(b.timestamp)
        if ts is None:
            raise ValueError("missing timestamp")
    except (ValidationError, ValueError, TypeError) as exc:
        raise _drift("clob/book", exc) from exc
    bids = sorted(
        (Level(price=x.price, size=x.size) for x in b.bids if x.size > 0), key=lambda lv: -lv.price
    )
    asks = sorted(
        (Level(price=x.price, size=x.size) for x in b.asks if x.size > 0), key=lambda lv: lv.price
    )
    return BookSnapshot(
        token_id=b.asset_id,
        ts=ts,
        bids=tuple(bids),
        asks=tuple(asks),
        tick_size=b.tick_size,
        min_order_size=b.min_order_size,
    )
