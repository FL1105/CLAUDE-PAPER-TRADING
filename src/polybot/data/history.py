"""Import historical trades from bulk datasets (CSV / Parquet / remote) via DuckDB.

Assumptions:
    * A dataset is turned into rows with the SAME column names as the Data API
      ``/trades`` payload (``proxyWallet, side, asset, conditionId, size, price,
      timestamp, outcomeIndex, transactionHash``). That is either already true
      ("canonical" files) or done by a user-supplied DuckDB SQL mapping with a
      ``{src}`` placeholder. The column layouts of public datasets are UNVERIFIED,
      so no mapping is hard-coded as "known". docs/HISTORY_IMPORT.md has
      templates and the inspect-first workflow.
    * ``conditionId`` / ``outcomeIndex`` may be NULL. They are filled from the
      ``tokens`` table after `polybot backfill --markets` looks the tokens up.

Failure modes guarded against:
    * Trusting a third-party dataset blindly: every row goes through the same
      adapter (``schemas.parse_trade``) as live API data. Bad rows are counted
      in ``schema_drift`` (endpoint ``import:<name>``), never silently fixed.
    * Downloading 100+ GB: the date window is pushed into the DuckDB query, so
      remote Parquet only reads what is needed.
    * Survivorship: the importer never filters by wallet or by PnL.
    * Re-import duplicates: inserts are idempotent by ``trade_key``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polybot.data import schemas
from polybot.data.store import Store

log = logging.getLogger("polybot.history")

API_COLUMNS = (
    "proxyWallet",
    "side",
    "asset",
    "conditionId",
    "size",
    "price",
    "timestamp",
    "outcomeIndex",
    "transactionHash",
)


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("History import needs DuckDB: pip install -e '.[history]'") from exc
    return duckdb


ID_COLUMNS = ("proxyWallet", "asset", "conditionId", "transactionHash", "side")
CSV_SUFFIXES = (".csv", ".csv.gz", ".tsv", ".txt")


def _source_sql(path: str) -> str:
    lit = "'" + path.replace("'", "''") + "'"
    if path.lower().endswith(CSV_SUFFIXES):
        # Never let CSV type sniffing touch IDs: "0xc" would be read as the integer 12.
        return f"read_csv({lit}, all_varchar=true)"
    return lit


def build_query(path: str, mapping_sql: str | None) -> str:
    """The SELECT that yields API-shaped rows. ``mapping_sql`` must contain ``{src}``."""
    src = _source_sql(path)
    if mapping_sql is None:
        return f"SELECT * FROM {src}"
    if "{src}" not in mapping_sql:
        raise ValueError("mapping SQL must contain the {src} placeholder")
    return mapping_sql.replace("{src}", src)


def inspect(path: str, mapping_sql: str | None = None, n: int = 5) -> dict[str, Any]:
    """Columns, types and a few sample rows (run this first on any new dataset)."""
    con = _duckdb().connect()
    rel = con.sql(f"SELECT * FROM ({build_query(path, mapping_sql)}) LIMIT {int(n)}")
    cols = list(zip(rel.columns, [str(t) for t in rel.types], strict=True))
    rows = rel.fetchall()
    missing = [c for c in API_COLUMNS if c not in rel.columns]
    return {
        "columns": cols,
        "sample": [dict(zip(rel.columns, r, strict=True)) for r in rows],
        "missing_api_columns": missing,
    }


@dataclass
class ImportResult:
    rows_read: int = 0
    inserted: int = 0
    drift: int = 0
    oldest_ts: int | None = None
    newest_ts: int | None = None


def import_trades(
    store: Store,
    path: str,
    *,
    name: str,
    start_ts: int,
    end_ts: int,
    mapping_sql: str | None = None,
    batch: int = 50_000,
) -> ImportResult:
    """Stream rows in [start_ts, end_ts) into the trades table."""
    q = build_query(path, mapping_sql)
    con = _duckdb().connect()
    rel = con.sql(f"SELECT * FROM ({q}) LIMIT 0")
    missing = [c for c in API_COLUMNS if c not in rel.columns]
    required_missing = [
        c for c in missing if c not in ("conditionId", "outcomeIndex", "transactionHash")
    ]
    if required_missing:
        raise ValueError(
            f"dataset lacks required columns {required_missing}; "
            "run `polybot history inspect` and supply a mapping"
        )
    # Push the time window down so remote Parquet reads only what is needed. Works
    # for epoch-second or TIMESTAMP columns; anything else fails loudly here.
    ts_type = str(rel.types[rel.columns.index("timestamp")]).upper()
    if "TIMESTAMP" in ts_type:
        ts_expr = 'epoch("timestamp")'
    elif "VARCHAR" in ts_type:
        ts_expr = (
            'coalesce(TRY_CAST("timestamp" AS BIGINT), epoch(TRY_CAST("timestamp" AS TIMESTAMP)))'
        )
    else:
        ts_expr = '"timestamp"'
    parts = []
    for c in API_COLUMNS:
        if c == "timestamp":
            parts.append(f'CAST({ts_expr} AS BIGINT) AS "timestamp"')
        elif c in rel.columns and c in ID_COLUMNS:
            parts.append(f'CAST("{c}" AS VARCHAR) AS "{c}"')
        elif c in rel.columns:
            parts.append(f'"{c}"')
        else:
            parts.append(f'NULL AS "{c}"')
    sql = (
        f"SELECT {', '.join(parts)} FROM ({q}) "
        f"WHERE {ts_expr} >= {int(start_ts)} AND {ts_expr} < {int(end_ts)}"
    )
    cur = con.execute(sql)
    res = ImportResult()
    endpoint = f"import:{name}"
    while True:
        chunk = cur.fetchmany(batch)
        if not chunk:
            break
        parsed = []
        for row in chunk:
            raw = dict(zip(API_COLUMNS, row, strict=True))
            try:
                t = schemas.parse_trade(raw, require_condition=False)
            except schemas.SchemaDriftError as exc:
                res.drift += 1
                store.log_drift(endpoint, str(exc))
                continue
            parsed.append(t)
            res.oldest_ts = t.ts if res.oldest_ts is None else min(res.oldest_ts, t.ts)
            res.newest_ts = t.ts if res.newest_ts is None else max(res.newest_ts, t.ts)
        res.rows_read += len(chunk)
        res.inserted += store.insert_trades(parsed, endpoint)
        log.info("imported %d rows (%d new) from %s", res.rows_read, res.inserted, name)
    store.backfill_trade_links()
    store.set_cursor(
        f"import:{name}",
        {
            "path": path,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "rows_read": res.rows_read,
            "inserted": res.inserted,
        },
    )
    return res


def read_mapping(path: str | Path | None) -> str | None:
    return None if path is None else Path(path).read_text(encoding="utf-8")
