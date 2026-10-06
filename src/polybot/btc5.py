"""BTC 5-minute Up/Down markets: record real asks near expiry, then measure a favourite-buy.

Lead from the dataset study (scripts/btc_updown_study.py): buying the favourite in the last
30 seconds returned about +0.1% to +2.3% net per week, but that used the LAST TRADE price,
which a real buyer may not get. This module records the real top of book of the Up token every
couple of seconds so the same question can be answered at the actual ask.

Slug pattern (verified 2026-10-06): ``btc-updown-5m-<window start epoch>``, window = 300 s.
Assumptions: we buy at the best ask visible ``lag_s`` seconds before the snapshot we act on
(default 2 s of latency), the favourite is whichever side has the higher mid, and the taker fee is
``rate * p * (1 - p)`` per share (verified formula, rate 0.07 for crypto).
Failure mode guarded against: a thin edge measured at a price nobody could trade.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from polybot.data import schemas

if TYPE_CHECKING:
    from polybot.data.client import PolymarketClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS btc5_windows (
  start_ts INTEGER PRIMARY KEY, end_ts INTEGER NOT NULL, up_token TEXT NOT NULL,
  condition_id TEXT, up_won INTEGER);
CREATE TABLE IF NOT EXISTS btc5_books (
  start_ts INTEGER NOT NULL, ts REAL NOT NULL, bid REAL, bid_sz REAL, ask REAL, ask_sz REAL);
CREATE INDEX IF NOT EXISTS btc5_books_w ON btc5_books(start_ts, ts);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _window(client: PolymarketClient, start: int, *, closed: bool = False) -> dict[str, Any] | None:
    # Gamma hides closed markets from slug lookups unless closed=true is passed (verified).
    extra = {"closed": "true"} if closed else {}
    raw = client.markets(slug=f"btc-updown-5m-{start}", **extra)
    if not isinstance(raw, list) or not raw:
        return None
    m = raw[0]
    toks = (
        json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
    )
    labels = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    if not toks or labels[0] != "Up":
        return None
    return {"cid": m.get("conditionId"), "up": str(toks[0]), "raw": m}


def resolve_pending(client: PolymarketClient, con: sqlite3.Connection, now: float) -> int:
    n = 0
    for (start,) in con.execute(
        "SELECT start_ts FROM btc5_windows WHERE up_won IS NULL AND end_ts < ?", (now - 90,)
    ).fetchall():
        w = _window(client, start, closed=True)
        if w is None or not w["raw"].get("closed"):
            continue
        try:
            prices = [float(x) for x in ast.literal_eval(str(w["raw"]["outcomePrices"]))]
            _, winner = schemas._resolution(
                True, [1.0 if p >= 0.99 else 0.0 if p <= 0.01 else p for p in prices]
            )
        except (ValueError, SyntaxError):
            continue
        if winner is not None:
            con.execute(
                "UPDATE btc5_windows SET up_won=? WHERE start_ts=?", (int(winner == 0), start)
            )
            n += 1
    con.commit()
    return n


def record(
    client: PolymarketClient,
    db: Path,
    *,
    duration_s: float,
    interval_s: float = 2.0,
    clock: Any = time.time,
    sleep: Any = time.sleep,
) -> dict[str, int]:
    con = open_db(db)
    t_end = clock() + duration_s
    books = 0
    last_resolve = 0.0
    while clock() < t_end:
        t0 = clock()
        start = int(t0) - int(t0) % 300
        try:
            row = con.execute(
                "SELECT up_token FROM btc5_windows WHERE start_ts=?", (start,)
            ).fetchone()
            if row is None:
                w = _window(client, start)
                if w is not None:
                    con.execute(
                        "INSERT OR IGNORE INTO btc5_windows VALUES (?,?,?,?,NULL)",
                        (start, start + 300, w["up"], w["cid"]),
                    )
                    row = (w["up"],)
            if row is not None:
                b = schemas.parse_book(client.book(row[0]))
                con.execute(
                    "INSERT INTO btc5_books VALUES (?,?,?,?,?,?)",
                    (
                        start,
                        t0,
                        b.best_bid,
                        b.bids[0].size if b.bids else None,
                        b.best_ask,
                        b.asks[0].size if b.asks else None,
                    ),
                )
                books += 1
            if t0 - last_resolve > 120:
                resolve_pending(client, con, t0)
                last_resolve = t0
            con.commit()
        except Exception:  # keep recording; gaps show up as missing snapshots
            pass
        sleep(max(0.0, interval_s - (clock() - t0)))
    resolve_pending(client, con, clock() + 200)
    con.close()
    return {"books": books}


def analyze(
    db: Path,
    *,
    offset_s: float = 30.0,
    lag_s: float = 2.0,
    fee_rate: float = 0.07,
    max_stale_s: float = 6.0,
) -> dict[str, float | int | None]:
    """Buy the favourite at the real ask seen ``offset_s`` before the end (acting after lag)."""
    con = open_db(db)
    n = wins = 0
    cost = pnl = 0.0
    spreads: list[float] = []
    for start, end, up_won in con.execute(
        "SELECT start_ts, end_ts, up_won FROM btc5_windows WHERE up_won IS NOT NULL"
    ).fetchall():
        t_dec = end - offset_s  # decide on the snapshot at t_dec ...
        seen = con.execute(
            "SELECT ts, bid, ask FROM btc5_books "
            "WHERE start_ts=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (start, t_dec),
        ).fetchone()
        if seen is None or t_dec - seen[0] > max_stale_s or seen[1] is None or seen[2] is None:
            continue
        up_fav = (seen[1] + seen[2]) / 2 >= 0.5
        # ... and the order lands lag_s later: pay the ask of the first snapshot at/after that
        fill = con.execute(
            "SELECT bid, ask FROM btc5_books "
            "WHERE start_ts=? AND ts>=? AND ts<? ORDER BY ts LIMIT 1",
            (start, t_dec + lag_s, end),
        ).fetchone()
        if fill is None or fill[0] is None or fill[1] is None:
            continue
        bid, ask = fill
        price = ask if up_fav else 1.0 - bid  # buying Down costs 1 - best bid of the Up token
        if not 0.0 < price < 1.0:
            continue
        won = bool(up_won) == up_fav
        fee = fee_rate * price * (1 - price)
        n += 1
        wins += won
        cost += price + fee
        pnl += (1.0 if won else 0.0) - price - fee
        spreads.append(ask - bid)
    con.close()
    return {
        "windows": n,
        "win_rate": wins / n if n else None,
        "roi_net_per_dollar": pnl / cost if cost else None,
        "avg_spread": sum(spreads) / len(spreads) if spreads else None,
    }
