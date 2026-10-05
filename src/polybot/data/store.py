"""SQLite storage: schema, idempotent upserts, append-only paper ledger.

Assumptions:
    * One local SQLite file (``storage.db_path``); standard library only.
    * Timestamps are UTC epoch seconds (INTEGER). Money is REAL (research-grade).
    * ``trade_key`` (see ``schemas.trade_key``) uniquely identifies a fill.

Failure modes guarded against:
    * Duplicates on re-run: every ingest write is an idempotent upsert, so a
      crashed or repeated backfill never double-counts trades.
    * Rewriting history: the paper_* tables are append-only. UPDATE and DELETE
      are blocked by triggers; corrections are new rows.
    * Silent data problems: ``anomalies`` and ``schema_drift`` tables record every
      dropped or suspicious row so the report (and gate 12) can surface them.
    * Losing progress: ``ingest_state`` stores resumable cursors per job.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from polybot.data.schemas import BookSnapshot, MarketRecord, TokenRecord, TradeRecord

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    end_ts INTEGER,
    resolved_ts INTEGER,
    resolution_status TEXT NOT NULL,
    winning_outcome_index INTEGER,
    settlement_prices TEXT NOT NULL DEFAULT '[]',
    category TEXT,
    neg_risk INTEGER NOT NULL DEFAULT 0,
    fee_coeff_at_trade_time REAL,
    updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    outcome_index INTEGER NOT NULL,
    outcome_label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tokens_cond ON tokens(condition_id);

CREATE TABLE IF NOT EXISTS trades (
    trade_key TEXT PRIMARY KEY,
    tx_hash TEXT,
    ts INTEGER NOT NULL,
    wallet TEXT NOT NULL,
    condition_id TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price REAL NOT NULL CHECK (price >= 0 AND price <= 1),
    size_shares REAL NOT NULL,
    size_usdc REAL NOT NULL,
    outcome_index INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_wallet ON trades(wallet, ts);
CREATE INDEX IF NOT EXISTS ix_trades_cond ON trades(condition_id);

CREATE TABLE IF NOT EXISTS book_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    fetched_ts INTEGER NOT NULL,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    tick_size REAL,
    UNIQUE (token_id, ts, fetched_ts)
);

CREATE TABLE IF NOT EXISTS paper_signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    wallet TEXT,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    wallet_trade_key TEXT,
    wallet_price REAL,
    status TEXT NOT NULL,
    skip_reason TEXT,
    evidence_mode TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    UNIQUE (strategy, wallet_trade_key)
);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES paper_signals(signal_id),
    created_ts INTEGER NOT NULL,
    wallet TEXT,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    wallet_price REAL,
    fill_price REAL NOT NULL,
    best_ask REAL NOT NULL,
    slippage REAL,              -- fill_price - wallet_price (NULL if no wallet price)
    slippage_vs_best REAL NOT NULL,
    fee REAL NOT NULL,
    fee_coeff REAL NOT NULL,
    intended_stake REAL NOT NULL,
    stake_usdc REAL NOT NULL,   -- USDC actually spent, excluding fee
    shares REAL NOT NULL,
    shadow_fills_json TEXT NOT NULL,
    book_snapshot_id INTEGER REFERENCES book_snapshots(snapshot_id),
    evidence_mode TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    UNIQUE (signal_id)
);

CREATE TABLE IF NOT EXISTS paper_settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id INTEGER NOT NULL REFERENCES paper_fills(fill_id),
    created_ts INTEGER NOT NULL,
    resolution_status TEXT NOT NULL,
    resolved_ts INTEGER,
    payout REAL NOT NULL,
    net_pnl REAL NOT NULL,
    supersedes INTEGER REFERENCES paper_settlements(settlement_id),
    config_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frozen_sets (
    epoch_start INTEGER PRIMARY KEY,
    epoch_end INTEGER NOT NULL,
    wallets_json TEXT NOT NULL,
    eligible_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    leakage_failures INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_state (
    job TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_drift (
    drift_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

APPEND_ONLY = ("paper_signals", "paper_fills", "paper_settlements", "frozen_sets")


def _append_only_triggers() -> str:
    parts = []
    for t in APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(parts)


class Store:
    """Thin wrapper around a SQLite connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")  # crash-safe, concurrent reads
        self.conn.executescript(DDL + _append_only_triggers())
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # --- markets / tokens ------------------------------------------------------
    def upsert_markets(self, items: Iterable[tuple[MarketRecord, list[TokenRecord]]]) -> int:
        now = int(time.time())
        n = 0
        with self.tx() as c:
            for m, toks in items:
                c.execute(
                    """INSERT INTO markets(condition_id, question, end_ts, resolved_ts,
                        resolution_status, winning_outcome_index, settlement_prices, category,
                        neg_risk, fee_coeff_at_trade_time, updated_ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(condition_id) DO UPDATE SET
                        question=excluded.question, end_ts=excluded.end_ts,
                        resolved_ts=excluded.resolved_ts,
                        resolution_status=excluded.resolution_status,
                        winning_outcome_index=excluded.winning_outcome_index,
                        settlement_prices=excluded.settlement_prices,
                        category=COALESCE(excluded.category, markets.category),
                        neg_risk=excluded.neg_risk,
                        fee_coeff_at_trade_time=COALESCE(excluded.fee_coeff_at_trade_time,
                                                         markets.fee_coeff_at_trade_time),
                        updated_ts=excluded.updated_ts""",
                    (
                        m.condition_id,
                        m.question,
                        m.end_ts,
                        m.resolved_ts,
                        m.resolution_status.value,
                        m.winning_outcome_index,
                        json.dumps(list(m.settlement_prices)),
                        m.category,
                        int(m.neg_risk),
                        m.fee_coeff_at_trade_time,
                        now,
                    ),
                )
                c.executemany(
                    """INSERT INTO tokens(token_id, condition_id, outcome_index, outcome_label)
                    VALUES (?,?,?,?) ON CONFLICT(token_id) DO NOTHING""",
                    [(t.token_id, t.condition_id, t.outcome_index, t.outcome_label) for t in toks],
                )
                n += 1
        return n

    # --- trades ----------------------------------------------------------------
    def insert_trades(self, trades: Iterable[TradeRecord], source: str) -> int:
        """Insert trades; existing trade_keys are ignored. Returns rows newly inserted."""
        rows = [
            (
                t.trade_key,
                t.tx_hash,
                t.ts,
                t.wallet,
                t.condition_id or None,
                t.token_id,
                t.side,
                t.price,
                t.size_shares,
                t.size_usdc,
                t.outcome_index,
                source,
            )
            for t in trades
        ]
        with self.tx() as c:
            before = c.total_changes
            c.executemany(
                """INSERT OR IGNORE INTO trades(trade_key, tx_hash, ts, wallet, condition_id,
                    token_id, side, price, size_shares, size_usdc, outcome_index, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            return c.total_changes - before

    def backfill_trade_links(self) -> int:
        """Fill missing condition_id / outcome_index on trades from the tokens table."""
        with self.tx() as c:
            before = c.total_changes
            c.execute(
                """UPDATE trades SET
                    condition_id = (SELECT condition_id FROM tokens t
                                    WHERE t.token_id = trades.token_id),
                    outcome_index = (SELECT outcome_index FROM tokens t
                                     WHERE t.token_id = trades.token_id)
                WHERE (condition_id IS NULL OR outcome_index IS NULL)
                  AND token_id IN (SELECT token_id FROM tokens)"""
            )
            return c.total_changes - before

    # --- books -----------------------------------------------------------------
    def insert_book(self, book: BookSnapshot, fetched_ts: int) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO book_snapshots(token_id, ts, fetched_ts, bids_json,
                    asks_json, tick_size) VALUES (?,?,?,?,?,?)""",
                (
                    book.token_id,
                    book.ts,
                    fetched_ts,
                    json.dumps([[lv.price, lv.size] for lv in book.bids]),
                    json.dumps([[lv.price, lv.size] for lv in book.asks]),
                    book.tick_size,
                ),
            )
            if cur.lastrowid and cur.rowcount:
                return int(cur.lastrowid)
            row = c.execute(
                "SELECT snapshot_id FROM book_snapshots WHERE token_id=? AND ts=? AND fetched_ts=?",
                (book.token_id, book.ts, fetched_ts),
            ).fetchone()
            return int(row[0])

    # --- paper ledger (append-only) ---------------------------------------------
    def signal_exists(self, strategy: str, wallet_trade_key: str | None) -> bool:
        if wallet_trade_key is None:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM paper_signals WHERE strategy=? AND wallet_trade_key=?",
            (strategy, wallet_trade_key),
        ).fetchone()
        return row is not None

    def record_signal_and_fill(
        self, signal: dict[str, Any], fill: dict[str, Any] | None
    ) -> tuple[int, int | None]:
        """Insert a signal (and its fill) in ONE transaction: crash-safe."""
        with self.tx() as c:
            cols = ", ".join(signal)
            cur = c.execute(
                f"INSERT INTO paper_signals({cols}) VALUES ({', '.join('?' * len(signal))})",
                tuple(signal.values()),
            )
            sid = int(cur.lastrowid or 0)
            fid = None
            if fill is not None:
                row = {**fill, "signal_id": sid}
                cur = c.execute(
                    f"INSERT INTO paper_fills({', '.join(row)}) "
                    f"VALUES ({', '.join('?' * len(row))})",
                    tuple(row.values()),
                )
                fid = int(cur.lastrowid or 0)
            return sid, fid

    def record_settlement(self, row: dict[str, Any]) -> int:
        with self.tx() as c:
            cur = c.execute(
                f"INSERT INTO paper_settlements({', '.join(row)}) "
                f"VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            return int(cur.lastrowid or 0)

    def fills_frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM paper_fills ORDER BY fill_id", self.conn)

    def signals_frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM paper_signals ORDER BY signal_id", self.conn)

    def settlements_frame(self) -> pd.DataFrame:
        """Effective settlements: a row superseded by a later correction is dropped."""
        return pd.read_sql_query(
            """SELECT * FROM paper_settlements s
               WHERE NOT EXISTS (SELECT 1 FROM paper_settlements c
                                 WHERE c.supersedes = s.settlement_id)
               ORDER BY settlement_id""",
            self.conn,
        )

    # --- meta / forward-run state ------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str, *, overwrite: bool = True) -> None:
        verb = "INSERT INTO meta(key, value) VALUES (?, ?) " + (
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            if overwrite
            else "ON CONFLICT(key) DO NOTHING"
        )
        with self.tx() as c:
            c.execute(verb, (key, value))

    def save_frozen_set(self, row: dict[str, Any]) -> bool:
        """Insert the frozen set for an epoch. Returns False if one already exists (frozen)."""
        with self.tx() as c:
            cur = c.execute(
                f"INSERT OR IGNORE INTO frozen_sets({', '.join(row)}) "
                f"VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            return cur.rowcount == 1

    def frozen_sets(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM frozen_sets ORDER BY epoch_start", self.conn)

    def max_trade_rowid(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM trades").fetchone()[0])

    def trades_after_rowid(self, rowid: int) -> pd.DataFrame:
        return pd.read_sql_query(
            """SELECT t.rowid AS rid, t.*, m.category FROM trades t
               LEFT JOIN markets m ON m.condition_id = t.condition_id
               WHERE t.rowid > ? ORDER BY t.ts, t.rowid""",
            self.conn,
            params=(rowid,),
        )

    # --- kill-switch halt (paper-mode enforcement) ----------------------------------
    def set_halt(self, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES ('halted', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (reason,),
            )
        self.log_anomaly("kill_switch", reason)

    def get_halt(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
        return None if row is None else str(row[0])

    def clear_halt(self, note: str) -> None:
        """Manual review only (CLI). The clearing note is kept in the anomaly log."""
        with self.tx() as c:
            c.execute("DELETE FROM meta WHERE key='halted'")
        self.log_anomaly("kill_switch_cleared", note)

    # --- bookkeeping -----------------------------------------------------------
    def get_cursor(self, job: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT cursor_json FROM ingest_state WHERE job=?", (job,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_cursor(self, job: str, cursor: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO ingest_state(job, cursor_json, updated_ts) VALUES (?,?,?)
                ON CONFLICT(job) DO UPDATE SET cursor_json=excluded.cursor_json,
                    updated_ts=excluded.updated_ts""",
                (job, json.dumps(cursor, sort_keys=True), int(time.time())),
            )

    def log_anomaly(self, kind: str, detail: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO anomalies(ts, kind, detail) VALUES (?,?,?)",
                (int(time.time()), kind, detail[:2000]),
            )

    def log_drift(self, endpoint: str, detail: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO schema_drift(ts, endpoint, detail) VALUES (?,?,?)",
                (int(time.time()), endpoint, detail[:2000]),
            )

    def anomaly_counts(self) -> dict[str, int]:
        return dict(
            self.conn.execute(
                "SELECT kind, COUNT(*) FROM anomalies GROUP BY kind ORDER BY kind"
            ).fetchall()
        )

    def drift_count_since(self, ts: int) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM schema_drift WHERE ts >= ?", (ts,)).fetchone()[
                0
            ]
        )

    def count(self, table: str) -> int:
        if table not in {
            "markets",
            "tokens",
            "trades",
            "book_snapshots",
            "paper_signals",
            "paper_fills",
            "paper_settlements",
            "anomalies",
            "schema_drift",
        }:
            raise ValueError(f"unknown table {table}")
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # --- analysis frames (the walk-forward input format) -------------------------
    def trades_frame(self) -> pd.DataFrame:
        """Trades in the analysis format: wallet, market, outcome, side, price, shares, ts."""
        df = pd.read_sql_query(
            """SELECT wallet, condition_id AS market, outcome_index AS outcome, side, price,
                      size_shares AS shares, ts, tx_hash, token_id
               FROM trades WHERE condition_id IS NOT NULL AND outcome_index IS NOT NULL
               ORDER BY ts, trade_key""",
            self.conn,
        )
        return df

    def markets_frame(self) -> pd.DataFrame:
        """Markets: market, status, winning_outcome (NaN unless resolved), resolved_ts, end_ts."""
        df = pd.read_sql_query(
            """SELECT condition_id AS market, resolution_status AS status,
                      winning_outcome_index AS winning_outcome, resolved_ts, end_ts, category,
                      settlement_prices
               FROM markets ORDER BY condition_id""",
            self.conn,
        )
        df.loc[df["status"] != "resolved", "winning_outcome"] = float("nan")
        return df
