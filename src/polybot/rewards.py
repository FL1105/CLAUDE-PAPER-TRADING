"""Liquidity-reward farming study: record real depth, estimate rewards, replay fills.

Polymarket pays daily USDC to makers whose resting orders are near the midpoint
(docs.polymarket.com/market-makers/liquidity-rewards, read 2026-10-08):
    order score = ((v - s) / v)^2 * size      v = max qualifying spread, s = distance from mid
    two-sided quotes count fully; single-sided count 1/3 (mid 0.10 to 0.90), nothing outside it
    book sampled once a minute; share = my score / all makers' scores; reward = share * daily pool.
This module answers: net of fill losses, is quoting for rewards worth it for a small quoter?

Assumptions (all stated, none hidden):
    * Competitors' scores come from recorded levels within v of the mid, aggregated as if one
      maker: Q = max(min(Qb, Qa), max(Qb, Qa) / 3). Real competitors are many and one-sided, so
      this is a rough estimate of our share (it could be too high or too low).
    * We quote both sides at the same distance d: a YES bid at mid - d and a NO bid, which is a YES
      ask at mid + d, with no inventory needed. Orders re-post every snapshot at the BACK of the
      queue (displayed size at or better than our price trades first). A taker trade strictly
      through our price fills us fully. Maker fee is 0; rebates are ignored.
    * Net inventory (long positive, short negative, in YES shares) is marked at the last mid.
    * Rewards are credited as pool * mean(share) * elapsed_days while we are quoting.
Failure mode guarded against: counting reward income without the inventory losses that earn it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from polybot.data import schemas

if TYPE_CHECKING:
    from polybot.data.client import PolymarketClient

SCHEMA = """
CREATE TABLE IF NOT EXISTS rw_markets (
  token TEXT PRIMARY KEY, cid TEXT, question TEXT, pool REAL, v REAL, min_size REAL);
CREATE TABLE IF NOT EXISTS rw_books (
  ts INTEGER NOT NULL, token TEXT NOT NULL, mid REAL, bids TEXT, asks TEXT);
CREATE TABLE IF NOT EXISTS rw_trades (
  key TEXT PRIMARY KEY, ts INTEGER NOT NULL, token TEXT NOT NULL,
  price REAL NOT NULL, size REAL NOT NULL, side TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS rw_books_t ON rw_books(token, ts);
CREATE INDEX IF NOT EXISTS rw_trades_t ON rw_trades(token, ts);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def order_score(v: float, s: float, size: float) -> float:
    return ((v - s) / v) ** 2 * size if 0.0 <= s <= v else 0.0


def competitor_score(
    bids: list[list[float]], asks: list[list[float]], mid: float, v: float
) -> float:
    qb = sum(order_score(v, mid - p, s) for p, s in bids)
    qa = sum(order_score(v, p - mid, s) for p, s in asks)
    if 0.10 <= mid <= 0.90:
        return max(min(qb, qa), max(qb, qa) / 3.0)
    return min(qb, qa)


def pick_markets(client: PolymarketClient, n: int) -> list[dict[str, Any]]:
    """Reward-paying binary markets (mid 0.10 to 0.90), ranked by pool per displayed depth."""
    raw = client.markets(
        closed="false", active="true", order="volume24hr", ascending="false", limit=300
    )
    out = []
    for r in raw if isinstance(raw, list) else []:
        try:
            m, toks = schemas.parse_gamma_market(r)
            pool = sum(float(x.get("rewardsDailyRate", 0)) for x in (r.get("clobRewards") or []))
            v = float(r.get("rewardsMaxSpread") or 0) / 100
            mid = (float(r["bestBid"]) + float(r["bestAsk"])) / 2
        except (schemas.SchemaDriftError, TypeError, ValueError, KeyError):
            continue
        if len(toks) != 2 or pool <= 0 or v <= 0 or not 0.10 <= mid <= 0.90:
            continue
        liq = float(r.get("liquidityNum") or 1.0)
        out.append(
            {
                "rank": pool / max(liq, 1.0),
                "token": toks[0].token_id,
                "cid": m.condition_id,
                "q": m.question[:80],
                "pool": pool,
                "v": v,
                "min_size": float(r.get("rewardsMinSize") or 0),
            }
        )
    out.sort(key=lambda d: -d["rank"])
    return out[:n]


def record(
    client: PolymarketClient,
    db: Path,
    markets: list[dict[str, Any]],
    *,
    duration_s: float,
    interval_s: float = 20.0,
    clock: Any = time.time,
    sleep: Any = time.sleep,
) -> dict[str, int]:
    con = open_db(db)
    for m in markets:
        con.execute(
            "INSERT OR REPLACE INTO rw_markets VALUES (?,?,?,?,?,?)",
            (m["token"], m["cid"], m["q"], m["pool"], m["v"], m["min_size"]),
        )
    t_end = clock() + duration_s
    nb = nt = 0
    while clock() < t_end:
        t0 = clock()
        for m in markets:
            try:
                b = schemas.parse_book(client.book(m["token"]))
                if b.best_bid is None or b.best_ask is None:
                    continue
                mid = (b.best_bid + b.best_ask) / 2
                keep_b = [[x.price, x.size] for x in b.bids if mid - x.price <= m["v"]]
                keep_a = [[x.price, x.size] for x in b.asks if x.price - mid <= m["v"]]
                con.execute(
                    "INSERT INTO rw_books VALUES (?,?,?,?,?)",
                    (int(clock()), m["token"], mid, json.dumps(keep_b), json.dumps(keep_a)),
                )
                nb += 1
                for r in client.trades(market=m["cid"], limit=100) or []:
                    if str(r.get("asset")) != m["token"]:
                        continue
                    key = f"{r.get('transactionHash')}:{r.get('timestamp')}:{r.get('size')}"
                    cur = con.execute(
                        "INSERT OR IGNORE INTO rw_trades VALUES (?,?,?,?,?,?)",
                        (
                            key,
                            int(r["timestamp"]),
                            m["token"],
                            float(r["price"]),
                            float(r["size"]),
                            str(r["side"]).upper(),
                        ),
                    )
                    nt += cur.rowcount
            except Exception:  # keep recording; gaps are visible in the data
                continue
        con.commit()
        sleep(max(0.0, interval_s - (clock() - t0)))
    con.close()
    return {"books": nb, "new_trades": nt}


@dataclass
class Result:
    reward: float = 0.0
    cash: float = 0.0
    inventory: float = 0.0
    last_mid: float = 0.5
    fills: int = 0
    volume: float = 0.0
    mean_share: float = 0.0
    elapsed_s: float = 0.0
    capital: float = 0.0

    @property
    def fill_pnl(self) -> float:
        return self.cash + self.inventory * self.last_mid


def simulate_market(
    books: list[tuple[int, float, list[list[float]], list[list[float]]]],
    trades: list[tuple[int, float, float, str]],
    pool: float,
    v: float,
    *,
    size: float,
    dist: float,
    max_inventory: float | None = None,
    pull_move: float | None = None,
) -> Result:
    """Two-sided quote of ``size`` shares at ``dist`` (price units) from the mid.

    ``max_inventory`` (shares, default ``size``) stops quoting the side that would add to an
    already full position. ``pull_move`` (price units): if the mid moved at least this much since
    the previous snapshot, quotes are pulled for the next interval (no fills, no reward), like a
    bot that steps back during fast moves.
    """
    cap = size if max_inventory is None else max_inventory
    res = Result(capital=size)  # about `size` dollars: a YES bid and a NO bid of `size` shares
    ti = 0
    shares: list[float] = []
    prev_mid: float | None = None
    for k, (ts, mid, bids, asks) in enumerate(books):
        res.last_mid = mid
        nxt = books[k + 1][0] if k + 1 < len(books) else ts
        pulled = pull_move is not None and prev_mid is not None and abs(mid - prev_mid) >= pull_move
        prev_mid = mid
        mine = 0.0 if pulled else order_score(v, dist, size)
        comp = competitor_score(bids, asks, mid, v)
        shares.append(mine / (mine + comp) if mine + comp > 0 else 0.0)
        if pulled:
            while ti < len(trades) and trades[ti][0] <= nxt:
                ti += 1
            continue
        bid_px, ask_px = round(mid - dist, 4), round(mid + dist, 4)
        q_bid = sum(s for p, s in bids if p >= bid_px - 1e-9)  # queue ahead at or better than ours
        q_ask = sum(s for p, s in asks if p <= ask_px + 1e-9)
        while ti < len(trades) and trades[ti][0] <= nxt:
            t_ts, price, tsz, side = trades[ti]
            ti += 1
            if t_ts <= ts:
                continue
            if side == "SELL" and price <= bid_px + 1e-9 and res.inventory < cap:
                if price < bid_px - 1e-9:
                    avail = tsz
                else:
                    take = min(tsz, q_bid)
                    q_bid -= take
                    avail = tsz - take
                q = min(avail, size)
                if q > 0:
                    res.cash -= q * bid_px
                    res.inventory += q
                    res.fills += 1
                    res.volume += q * bid_px
            elif side == "BUY" and price >= ask_px - 1e-9 and res.inventory > -cap:
                if price > ask_px + 1e-9:
                    avail = tsz
                else:
                    take = min(tsz, q_ask)
                    q_ask -= take
                    avail = tsz - take
                q = min(avail, size)
                if q > 0:
                    res.cash += q * ask_px
                    res.inventory -= q
                    res.fills += 1
                    res.volume += q * ask_px
    if books:
        res.elapsed_s = max(1.0, books[-1][0] - books[0][0])
        res.mean_share = sum(shares) / len(shares)
        res.reward = pool * res.mean_share * res.elapsed_s / 86400.0
    return res


def simulate(
    db: Path, *, size: float, dist_cents: float, pull_move_cents: float | None = None
) -> list[dict[str, Any]]:
    con = open_db(db)
    meta = {
        t: (q, pool, v)
        for t, q, pool, v in con.execute("SELECT token, question, pool, v FROM rw_markets")
    }
    books: dict[str, list[Any]] = defaultdict(list)
    for ts, tok, mid, b, a in con.execute(
        "SELECT ts, token, mid, bids, asks FROM rw_books ORDER BY ts"
    ):
        books[tok].append((ts, mid, json.loads(b), json.loads(a)))
    trades: dict[str, list[Any]] = defaultdict(list)
    for ts, tok, price, sz, side in con.execute(
        "SELECT ts, token, price, size, side FROM rw_trades ORDER BY ts"
    ):
        trades[tok].append((ts, price, sz, side))
    out = []
    for tok, bk in books.items():
        q, pool, v = meta[tok]
        pm = None if pull_move_cents is None else pull_move_cents / 100
        r = simulate_market(
            bk, trades.get(tok, []), pool, v, size=size, dist=dist_cents / 100, pull_move=pm
        )
        out.append(
            {
                "market": q[:50],
                "hours": r.elapsed_s / 3600,
                "pool_per_day": pool,
                "share": r.mean_share,
                "reward_usd": r.reward,
                "fill_pnl_usd": r.fill_pnl,
                "net_usd": r.reward + r.fill_pnl,
                "fills": r.fills,
                "net_per_day_per_100": (r.reward + r.fill_pnl)
                / (r.elapsed_s / 86400)
                / (size / 100),
            }
        )
    con.close()
    return out
