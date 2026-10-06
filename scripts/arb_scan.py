#!/usr/bin/env python3
"""Live scan: do the outcomes of one mutually exclusive event add up to $1 after fees?

For each negative-risk event with a COMPLETE outcome set (not 'augmented', which can gain
outcomes later) buying every YES at its best ask pays exactly $1, and selling every YES at its
best bid is the mirror. Net of the documented taker fee (rate * p * (1 - p) per share, per
market feeSchedule) the basket is an arbitrage only if cost < 1 (or proceeds > 1).
Top-of-book prices come from Gamma (can lag); candidates are re-checked against CLOB books with
sizes. Usage: python scripts/arb_scan.py [--min-edge 0.0]
"""

from __future__ import annotations

import argparse
import json

import httpx

G = "https://gamma-api.polymarket.com"
C = "https://clob.polymarket.com"
ap = argparse.ArgumentParser()
ap.add_argument("--min-edge", type=float, default=0.0)
ap.add_argument("--pages", type=int, default=5)
a = ap.parse_args()


def fee(m: dict, p: float) -> float:
    fs = m.get("feeSchedule") or {}
    return float(fs.get("rate", 0.0)) * p * (1 - p) if m.get("feesEnabled") else 0.0


events: list[dict] = []
for off in range(0, 100 * a.pages, 100):
    events += httpx.get(
        f"{G}/events",
        params={
            "closed": "false",
            "active": "true",
            "order": "volume24hr",
            "ascending": "false",
            "limit": 100,
            "offset": off,
        },
        timeout=40,
    ).json()
res = []
for e in events:
    if not e.get("negRisk") or e.get("negRiskAugmented"):
        continue
    ms = [m for m in e["markets"] if m.get("active") and not m.get("closed")]
    if len(ms) < 3 or not all(m.get("enableOrderBook") for m in ms):
        continue
    asks = [m.get("bestAsk") for m in ms]
    bids = [m.get("bestBid") for m in ms]
    if all(x is not None for x in asks):
        cost = sum(float(x) + fee(m, float(x)) for x, m in zip(asks, ms, strict=True))
        res.append(("BUY_ALL", e["title"][:50], len(ms), round(cost, 4), 1 - cost, e))
    if all(x is not None for x in bids):
        proceeds = sum(float(x) - fee(m, float(x)) for x, m in zip(bids, ms, strict=True))
        res.append(("SELL_ALL", e["title"][:50], len(ms), round(proceeds, 4), proceeds - 1, e))
print(f"{len(events)} events scanned, {len({r[1] for r in res})} complete negRisk sets with quotes")
res.sort(key=lambda r: -r[4])
for kind, title, n, val, edge, _e in res[:10]:
    print(f"{kind:8} edge {edge:+.4f}  value {val:.4f}  n={n}  {title}")
good = [r for r in res if r[4] > a.min_edge]
print(f"{len(good)} baskets with a positive net edge at Gamma top-of-book")
for kind, title, _n, _val, _edge, e in good[:5]:
    sizes = []
    for m in e["markets"]:
        toks = (
            json.loads(m["clobTokenIds"])
            if isinstance(m["clobTokenIds"], str)
            else m["clobTokenIds"]
        )
        b = httpx.get(f"{C}/book", params={"token_id": toks[0]}, timeout=20).json()
        side = b["asks"] if kind == "BUY_ALL" else b["bids"]
        best = (
            min(side, key=lambda x: float(x["price"]))
            if kind == "BUY_ALL" and side
            else (max(side, key=lambda x: float(x["price"])) if side else None)
        )
        sizes.append(float(best["size"]) if best else 0.0)
    print(f"  {title}: smallest top-of-book size across legs = {min(sizes):.1f} shares")
