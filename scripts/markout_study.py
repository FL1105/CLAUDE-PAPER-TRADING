#!/usr/bin/env python3
"""Taker markout vs the same token's price H seconds later (all wallets, one date window).

Maker edge per share is about -(taker markout) before fees and rebates, so this estimates
what passive liquidity provision earned/lost to adverse selection. Takers and makers are
zero-sum before fees: a negative taker markout means makers captured spread net of drift.
Usage: python scripts/markout_study.py --start 2026-09-01 --end 2026-09-03 [--horizon 300]
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import duckdb

SRC = "hf://datasets/SII-WANGZJ/Polymarket_data/trades.parquet"


def ts(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


ap = argparse.ArgumentParser()
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--horizon", type=int, default=300)
a = ap.parse_args()
con = duckdb.connect()
con.execute("LOAD httpfs")
s, e, h = ts(a.start), ts(a.end), a.horizon
con.execute(
    f"""CREATE TEMP TABLE f AS SELECT asset_id, timestamp AS t, price, usd_amount, taker_direction
        FROM read_parquet('{SRC}')
        WHERE timestamp >= {s} AND timestamp < {e + h} AND usd_amount > 0"""
)
q = f"""
WITH base AS (SELECT * FROM f WHERE t < {e}),
j AS (
  SELECT b.price AS p0, b.usd_amount AS usd, b.taker_direction AS dir, l.price AS p1
  FROM base b ASOF JOIN f l ON b.asset_id = l.asset_id AND l.t >= b.t + {h}
)
SELECT CASE WHEN p0 < .1 THEN '0 <.10' WHEN p0 < .3 THEN '1 .10-.30' WHEN p0 < .7 THEN '2 .30-.70'
            WHEN p0 < .9 THEN '3 .70-.90' ELSE '4 >=.90' END AS bucket,
       count(*) AS n,
       sum(usd) AS usd,
       sum(usd * (CASE WHEN dir='BUY' THEN p1 - p0 ELSE p0 - p1 END) / p0) / sum(usd)
         AS taker_markout_per_dollar
FROM j GROUP BY ALL ORDER BY 1
"""
print(con.sql(q))
print(
    con.sql(
        f"""WITH base AS (SELECT * FROM f WHERE t < {e}), j AS (
  SELECT b.price p0, b.usd_amount usd, b.taker_direction dir, l.price p1
  FROM base b ASOF JOIN f l ON b.asset_id=l.asset_id AND l.t >= b.t + {h})
  SELECT count(*) n, sum(usd * (CASE WHEN dir='BUY' THEN p1-p0 ELSE p0-p1 END)/p0)/sum(usd)
  AS overall_taker_markout_per_dollar FROM j"""
    )
)
