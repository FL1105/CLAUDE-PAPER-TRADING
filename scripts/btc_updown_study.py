#!/usr/bin/env python3
"""Are 'Bitcoin Up or Down' prices calibrated shortly before expiry? Is there a net edge?

For each resolved market that ends in [start, end), take the last fill at least X seconds
before the end and read the implied P(Up). Then buy the favourite at that price (a taker who
sees the same tape) and report win rate vs price and ROI net of an assumed parabolic fee.
Usage: python scripts/btc_updown_study.py --start 2026-09-20 --end 2026-09-27
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import duckdb

SRC = "hf://datasets/SII-WANGZJ/Polymarket_data"


def ts(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


ap = argparse.ArgumentParser()
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--fee", type=float, default=0.05, help="parabolic fee coefficient (assumption)")
a = ap.parse_args()
s, e = ts(a.start), ts(a.end)
con = duckdb.connect()
con.execute("LOAD httpfs")
con.execute(
    f"""CREATE TEMP TABLE m AS SELECT condition_id, token1, epoch(end_date) AS e,
        outcome_prices = '[''1'', ''0'']' AS up_won
        FROM read_parquet('{SRC}/markets.parquet')
        WHERE closed = 1 AND question ILIKE '%bitcoin up or down%' AND answer1 = 'Up'
          AND outcome_prices IN ('[''1'', ''0'']', '[''0'', ''1'']')
          AND epoch(end_date) >= {s} AND epoch(end_date) < {e}"""
)
con.execute(
    f"""CREATE TEMP TABLE t AS SELECT condition_id, asset_id, timestamp AS t, price
        FROM read_parquet('{SRC}/trades.parquet')
        WHERE timestamp >= {s - 3600} AND timestamp < {e}
          AND condition_id IN (SELECT condition_id FROM m)"""
)
for off in (300, 120, 30):
    q = f"""
    WITH last AS (
      SELECT m.condition_id, m.up_won,
             arg_max(CASE WHEN t.asset_id = m.token1 THEN t.price ELSE 1 - t.price END, t.t) AS p_up
      FROM m JOIN t USING (condition_id) WHERE t.t <= m.e - {off} GROUP BY ALL),
    fav AS (
      SELECT CASE WHEN p_up >= .5 THEN p_up ELSE 1 - p_up END AS price,
             CASE WHEN p_up >= .5 THEN up_won ELSE NOT up_won END AS win FROM last)
    SELECT '{off}s before end' AS at, round(price * 20) / 20 AS bucket, count(*) AS n,
           round(avg(price), 4) AS implied, round(avg(win::DOUBLE), 4) AS win_rate,
           round(avg((win::DOUBLE - price - {a.fee} * price * (1 - price)) / price), 4) AS net_roi
    FROM fav GROUP BY ALL ORDER BY bucket"""
    print(con.sql(q))
    print(
        con.sql(f"""WITH last AS (SELECT m.up_won,
      arg_max(CASE WHEN t.asset_id = m.token1 THEN t.price ELSE 1 - t.price END, t.t) AS p_up
      FROM m JOIN t USING (condition_id) WHERE t.t <= m.e - {off}
      GROUP BY m.condition_id, m.up_won),
      fav AS (SELECT CASE WHEN p_up>=.5 THEN p_up ELSE 1-p_up END price,
        CASE WHEN p_up>=.5 THEN up_won ELSE NOT up_won END win FROM last)
      SELECT count(*) n, round(avg((win::DOUBLE - price - {a.fee}*price*(1-price))/price),4)
      AS overall_net_roi_buy_favourite FROM fav""")
    )
