"""Passive market-making research: record real books + tape, replay a conservative maker.

Why: the taker markout study (docs/RESULTS_2026-10-06.md) says liquidity providers capture
about 1.7% of notional on average, but wallet copying and directional taking do not pay. This
module tests what a retail-sized passive quoter would actually have earned from REAL books
and trades, without assuming fills.

Assumptions (all chosen to understate profit):
    * Long-only: we quote a bid, and an ask only against inventory we already hold.
    * Orders are re-posted at every book snapshot and join the BACK of the queue: the
      displayed size at our price must trade first before we fill.
    * A taker trade strictly through our price fills us fully (no queue).
    * No maker rebates, no fee refunds; the maker fee is 0.
    * Inventory is marked at the last mid. No market resolved during the run is credited
      with its settlement, so tail losses are only visible if the price moved.
    * Snapshots are sampled every few seconds: fills between snapshots are credited to the
      order resting at the previous snapshot only.

Failure mode guarded against: believing a markout average is capturable. Adverse selection
shows up here because fills only happen when the real tape traded at our price.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from polybot.data import schemas

if TYPE_CHECKING:
    from polybot.data.client import PolymarketClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS mm_books (
  ts INTEGER NOT NULL, token TEXT NOT NULL, bid REAL, bid_sz REAL, ask REAL, ask_sz REAL);
CREATE TABLE IF NOT EXISTS mm_trades (
  key TEXT PRIMARY KEY, ts INTEGER NOT NULL, token TEXT NOT NULL,
  price REAL NOT NULL, size REAL NOT NULL, side TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mm_books_t ON mm_books(token, ts);
CREATE INDEX IF NOT EXISTS mm_trades_t ON mm_trades(token, ts);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


# --- recording -------------------------------------------------------------------------


def liquid_tokens(
    client: PolymarketClient, n: int, *, min_spread: float = 0.01, min_volume_24h: float = 20_000.0
) -> list[tuple[str, str]]:
    """(condition_id, token_id) of open binary markets worth quoting: wide enough and active.

    Ranked by 24h volume x quoted spread (a proxy for spread income on offer), so the very
    tightest, professionally-quoted books are skipped.
    """
    raw = client.markets(
        closed="false", active="true", order="volume24hr", ascending="false", limit=300
    )
    cands: list[tuple[float, str, str]] = []
    for r in raw if isinstance(raw, list) else []:
        try:
            m, toks = schemas.parse_gamma_market(r)
            spread = float(r.get("spread") or 0.0)
            vol = float(r.get("volume24hr") or 0.0)
        except (schemas.SchemaDriftError, TypeError, ValueError):
            continue
        if len(toks) != 2 or m.resolution_status != schemas.ResolutionStatus.OPEN:
            continue
        if spread + 1e-9 < min_spread or vol < min_volume_24h:
            continue
        cands.append((vol * spread, m.condition_id, toks[0].token_id))
    cands.sort(reverse=True)
    return [(c, t) for _, c, t in cands[:n]]


def record(
    client: PolymarketClient,
    db: Path,
    markets: list[tuple[str, str]],
    *,
    duration_s: float,
    interval_s: float = 5.0,
    clock: Any = time.time,
    sleep: Any = time.sleep,
) -> dict[str, int]:
    con = open_db(db)
    t_end = clock() + duration_s
    n_books = n_trades = 0
    while clock() < t_end:
        t0 = clock()
        for cid, tok in markets:
            try:
                b = schemas.parse_book(client.book(tok))
                con.execute(
                    "INSERT INTO mm_books VALUES (?,?,?,?,?,?)",
                    (
                        int(clock()),
                        tok,
                        b.best_bid,
                        b.bids[0].size if b.bids else None,
                        b.best_ask,
                        b.asks[0].size if b.asks else None,
                    ),
                )
                n_books += 1
                for r in client.trades(market=cid, limit=100) or []:
                    if str(r.get("asset")) != tok:
                        continue
                    key = f"{r.get('transactionHash')}:{r.get('timestamp')}:{r.get('size')}"
                    cur = con.execute(
                        "INSERT OR IGNORE INTO mm_trades VALUES (?,?,?,?,?,?)",
                        (
                            key,
                            int(r["timestamp"]),
                            tok,
                            float(r["price"]),
                            float(r["size"]),
                            str(r["side"]).upper(),
                        ),
                    )
                    n_trades += cur.rowcount
            except Exception:  # keep recording; gaps are visible in the data
                continue
        con.commit()
        sleep(max(0.0, interval_s - (clock() - t0)))
    con.close()
    return {"books": n_books, "new_trades": n_trades}


# --- replay ----------------------------------------------------------------------------


@dataclass
class MakerParams:
    quote_usd: float = 10.0
    max_inventory_usd: float = 50.0
    min_spread: float = 0.02  # only quote when the book is at least this wide
    tick: float = 0.01


@dataclass
class TokenResult:
    fills_buy: int = 0
    fills_sell: int = 0
    volume_usd: float = 0.0
    cash: float = 0.0
    inventory: float = 0.0
    last_mid: float | None = None
    max_inventory_usd: float = 0.0

    @property
    def pnl(self) -> float:
        return self.cash + self.inventory * (self.last_mid or 0.0)


def simulate_token(
    books: list[tuple[int, float | None, float | None, float | None, float | None]],
    trades: list[tuple[int, float, float, str]],
    p: MakerParams,
) -> TokenResult:
    """books: (ts, bid, bid_sz, ask, ask_sz) ascending; trades: (ts, price, size, side)."""
    res = TokenResult()
    ti = 0
    for k, (ts, bid, bid_sz, ask, ask_sz) in enumerate(books):
        if bid is not None and ask is not None:
            res.last_mid = (bid + ask) / 2
        nxt = books[k + 1][0] if k + 1 < len(books) else ts
        window = []
        while ti < len(trades) and trades[ti][0] <= nxt:
            if trades[ti][0] > ts:
                window.append(trades[ti])
            ti += 1
        if bid is None or ask is None or ask - bid < p.min_spread - 1e-9 or not window:
            continue
        # our resting orders for this window (placed at the snapshot, back of the queue)
        my_bid = round(bid, 4)
        my_ask = round(ask, 4)
        bid_queue = bid_sz or 0.0
        ask_queue = ask_sz or 0.0
        for _, price, size, side in window:
            if side == "SELL" and price <= my_bid + 1e-9:  # taker sells into the bids
                inv_usd = res.inventory * my_bid
                if inv_usd >= p.max_inventory_usd:
                    continue
                if price < my_bid - 1e-9:
                    avail = size
                else:
                    take = min(size, bid_queue)
                    bid_queue -= take
                    avail = size - take
                if avail <= 0:
                    continue
                want = p.quote_usd / my_bid
                q = min(avail, want, (p.max_inventory_usd - inv_usd) / my_bid)
                if q > 0:
                    res.cash -= q * my_bid
                    res.inventory += q
                    res.fills_buy += 1
                    res.volume_usd += q * my_bid
            elif side == "BUY" and price >= my_ask - 1e-9 and res.inventory > 0:
                if price > my_ask + 1e-9:
                    avail = size
                else:
                    take = min(size, ask_queue)
                    ask_queue -= take
                    avail = size - take
                if avail <= 0:
                    continue
                q = min(avail, res.inventory, p.quote_usd / my_ask)
                res.cash += q * my_ask
                res.inventory -= q
                res.fills_sell += 1
                res.volume_usd += q * my_ask
            res.max_inventory_usd = max(res.max_inventory_usd, res.inventory * my_bid)
    return res


@dataclass
class SimReport:
    per_token: dict[str, TokenResult] = field(default_factory=dict)

    def summary(self) -> dict[str, float]:
        r = self.per_token.values()
        vol = sum(x.volume_usd for x in r)
        pnl = sum(x.pnl for x in r)
        return {
            "tokens": len(self.per_token),
            "fills": sum(x.fills_buy + x.fills_sell for x in r),
            "volume_usd": vol,
            "pnl_usd": pnl,
            "pnl_per_dollar_traded": pnl / vol if vol else 0.0,
            "tokens_profitable": sum(1 for x in r if x.pnl > 0),
            "worst_token_pnl": min((x.pnl for x in r), default=0.0),
            "max_inventory_usd": max((x.max_inventory_usd for x in r), default=0.0),
        }


def simulate(db: Path, params: MakerParams) -> SimReport:
    con = open_db(db)
    books: dict[str, list[Any]] = defaultdict(list)
    for ts, tok, bid, bsz, ask, asz in con.execute(
        "SELECT ts, token, bid, bid_sz, ask, ask_sz FROM mm_books ORDER BY ts"
    ):
        books[tok].append((ts, bid, bsz, ask, asz))
    trades: dict[str, list[Any]] = defaultdict(list)
    for ts, tok, price, size, side in con.execute(
        "SELECT ts, token, price, size, side FROM mm_trades ORDER BY ts"
    ):
        trades[tok].append((ts, price, size, side))
    rep = SimReport()
    for tok, b in books.items():
        rep.per_token[tok] = simulate_token(b, trades.get(tok, []), params)
    con.close()
    return rep
