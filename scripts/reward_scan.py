#!/usr/bin/env python3
"""Estimate daily liquidity-reward income for a small two-sided quoter, from live books.

Rules (docs.polymarket.com/market-makers/liquidity-rewards, read 2026-10-08): score of a resting
order = ((v - s) / v)^2 * size, with v the market's max qualifying spread and s the distance from
the midpoint; sampled once a minute; share = my score / all makers' scores; two-sided quotes
count fully, single-sided count 1/3 (mid 0.10-0.90) or nothing (outside it).
Assumptions: competitors' aggregate scores come from the YES book (levels within v of the mid);
competitor Q_min = max(min(Qb, Qa), max(Qb, Qa) / 3) as if one maker (an approximation);
my quote sits d cents from the mid on both sides; capital needed is about 2 * size.
Not modelled: fills / adverse selection, the minimum-size rule per order, competition changes.
Usage: python scripts/reward_scan.py [--size 200] [--dist 1.0]
"""

from __future__ import annotations

import argparse
import json

import httpx

G, C = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
ap = argparse.ArgumentParser()
ap.add_argument("--size", type=float, default=200.0, help="shares quoted on each side")
ap.add_argument("--dist", type=float, default=1.0, help="distance from mid in cents")
a = ap.parse_args()


def score(v: float, s: float, size: float) -> float:
    return ((v - s) / v) ** 2 * size if 0 <= s <= v else 0.0


mk = httpx.get(
    f"{G}/markets",
    params={
        "closed": "false",
        "active": "true",
        "order": "volume24hr",
        "ascending": "false",
        "limit": 300,
    },
    timeout=40,
).json()
rows = []
for m in mk:
    cr = m.get("clobRewards") or []
    pool = sum(float(x.get("rewardsDailyRate", 0)) for x in cr)
    if pool <= 0 or not m.get("rewardsMaxSpread"):
        continue
    toks = (
        json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
    )
    b = httpx.get(f"{C}/book", params={"token_id": toks[0]}, timeout=20).json()
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    if not bids or not asks:
        continue
    mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
    v = float(m["rewardsMaxSpread"]) / 100
    qb = sum(score(v, mid - p, s) for p, s in bids)
    qa = sum(score(v, p - mid, s) for p, s in asks)
    two = 0.10 <= mid <= 0.90
    comp = max(min(qb, qa), max(qb, qa) / 3) if two else min(qb, qa)
    mine = score(v, a.dist / 100, a.size)  # two-sided: min(Q_one, Q_two) = one side's score
    share = mine / (mine + comp) if mine + comp > 0 else 0.0
    cap = a.size * mid + a.size * (1 - mid)  # bid YES + bid NO, about `size` dollars
    day = pool * share
    rows.append(
        (
            day,
            share,
            pool,
            mid,
            v * 100,
            float(m.get("rewardsMinSize") or 0),
            cap,
            m["question"][:44],
        )
    )
rows.sort(reverse=True)
print(f"{len(rows)} reward markets; quoting {a.size:.0f} shares per side {a.dist}c from mid")
print(f"{'$/day':>7} {'share':>6} {'pool':>6} {'mid':>5} {'v(c)':>5} {'min':>5} {'APR':>6}  market")
for day, share, pool, mid, v, mn, cap, q in rows[:12]:
    apr = day * 365 / cap if cap else 0
    print(
        f"{day:7.2f} {100 * share:5.1f}% {pool:6.0f} {mid:5.2f} {v:5.1f} {mn:5.0f} {100 * apr:5.0f}%  {q}"
    )
tot = sum(r[0] for r in rows[:5])
print(f"top 5 markets together: ${tot:.2f}/day on about ${sum(r[6] for r in rows[:5]):.0f} capital")
