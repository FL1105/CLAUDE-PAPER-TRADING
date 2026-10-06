#!/usr/bin/env python3
"""Shortlist wallets from the SII-WANGZJ Polymarket dataset by ACTIVITY only (never PnL).

The full 90-day window is ~200M rows: too many for the local SQLite store. This keeps the
wallets that could pass the bot's own activity filters (config/default.yaml `filters`):
>= min_resolved_markets distinct markets, >= min_active_span_days of activity and
<= max_trades_per_day. Selecting on activity, not outcomes, adds no look-ahead bias.

Usage: python scripts/hf_shortlist.py --start 2026-07-06 --end 2026-10-05
Wallets are then taken in hash order (a sample unrelated to performance) so the
import stays small enough for SQLite. Writes out/hf/wallets.parquet (column: wallet).
Needs the DuckDB httpfs extension.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = "hf://datasets/SII-WANGZJ/Polymarket_data/trades.parquet"


def ts(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


ap = argparse.ArgumentParser()
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--max-trades", type=int, default=1000, help="drop bot-like wallets")
ap.add_argument("--sample", type=int, default=10000, help="deterministic hash sample size")
ap.add_argument("--out", default=str(ROOT / "out/hf/wallets.parquet"))
a = ap.parse_args()
f = yaml.safe_load((ROOT / "config/default.yaml").read_text())["filters"]
con = duckdb.connect()
con.execute("LOAD httpfs")
con.execute(
    f"""COPY (
  SELECT taker AS wallet, count(distinct condition_id) AS markets, count(*) AS trades,
         (max(timestamp) - min(timestamp)) / 86400.0 AS span_days
  FROM read_parquet('{SRC}')
  WHERE timestamp >= {ts(a.start)} AND timestamp < {ts(a.end)}
  GROUP BY taker
  HAVING count(distinct condition_id) >= {int(f["min_resolved_markets"])}
     AND (max(timestamp) - min(timestamp)) / 86400.0 >= {int(f["min_active_span_days"])}
     AND count(*) / greatest(1.0, (max(timestamp) - min(timestamp)) / 86400.0)
         <= {int(f["max_trades_per_day"])}
     AND count(*) <= {int(a.max_trades)}
  ORDER BY hash(taker) LIMIT {int(a.sample)}
) TO '{a.out}' (FORMAT parquet)"""
)
n, t = con.sql(f"SELECT count(*), sum(trades) FROM read_parquet('{a.out}')").fetchone()
print(f"wallets={n} trades_in_window={t}")
