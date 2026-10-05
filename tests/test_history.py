from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from polybot.data.history import build_query, import_trades, inspect
from polybot.data.store import Store

W = "0x" + "1" * 40


def canonical(n: int = 10, start: int = 1_700_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "proxyWallet": [W] * n,
            "side": ["BUY"] * n,
            "asset": ["555"] * n,
            "conditionId": ["0xc"] * n,
            "size": [10.0] * n,
            "price": [0.4] * n,
            "timestamp": [start + i * 86_400 for i in range(n)],
            "outcomeIndex": [0] * n,
            "transactionHash": [f"0x{i}" for i in range(n)],
        }
    )


def test_canonical_csv_window_and_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "t.csv"
    canonical().to_csv(p, index=False)
    st = Store(":memory:")
    r = import_trades(
        st,
        str(p),
        name="csv",
        start_ts=1_700_000_000 + 2 * 86_400,
        end_ts=1_700_000_000 + 5 * 86_400,
    )
    assert r.inserted == 3 and r.rows_read == 3
    assert import_trades(st, str(p), name="csv", start_ts=0, end_ts=2**40).inserted == 7
    assert st.get_cursor("import:csv")["rows_read"] == 10


def test_parquet_with_timestamp_type_and_missing_optional_columns(tmp_path: Path) -> None:
    df = canonical(4).drop(columns=["conditionId", "outcomeIndex"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    p = tmp_path / "t.parquet"
    df.to_parquet(p)
    st = Store(":memory:")
    r = import_trades(st, str(p), name="pq", start_ts=0, end_ts=2**40)
    assert r.inserted == 4 and r.oldest_ts == 1_700_000_000
    row = st.conn.execute("SELECT condition_id FROM trades LIMIT 1").fetchone()
    assert row[0] is None  # to be linked via tokens later, never guessed


def test_mapping_sql_and_inspect(tmp_path: Path) -> None:
    raw = pd.DataFrame(
        {
            "user": [W, W],
            "dir": ["buy", "sell"],
            "tok": ["9", "9"],
            "qty": [5.0, 2.0],
            "px": [0.3, 0.6],
            "t": [1_700_000_000, 1_700_000_100],
        }
    )
    p = tmp_path / "raw.csv"
    raw.to_csv(p, index=False)
    mapping = (
        'SELECT "user" AS proxyWallet, upper(dir) AS side, CAST(tok AS VARCHAR) AS asset,'
        ' qty AS size, px AS price, t AS "timestamp" FROM {src}'
    )
    info = inspect(str(p), mapping)
    assert set(info["missing_api_columns"]) == {"conditionId", "outcomeIndex", "transactionHash"}
    st = Store(":memory:")
    assert (
        import_trades(st, str(p), name="m", start_ts=0, end_ts=2**40, mapping_sql=mapping).inserted
        == 2
    )


def test_missing_required_columns_rejected(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    canonical().drop(columns=["price"]).to_csv(p, index=False)
    with pytest.raises(ValueError, match="price"):
        import_trades(Store(":memory:"), str(p), name="b", start_ts=0, end_ts=2**40)
    with pytest.raises(ValueError, match="placeholder"):
        build_query("x.csv", "SELECT 1")


def test_bad_rows_counted_as_drift(tmp_path: Path) -> None:
    df = canonical(5)
    df.loc[1, "side"] = "HOLD"
    df.loc[2, "price"] = 1.7
    p = tmp_path / "t.csv"
    df.to_csv(p, index=False)
    st = Store(":memory:")
    r = import_trades(st, str(p), name="d", start_ts=0, end_ts=2**40)
    assert r.drift == 2 and r.inserted == 3 and st.count("schema_drift") == 2


def test_hex_ids_never_reinterpreted(tmp_path: Path) -> None:
    df = canonical(2)
    df["conditionId"] = ["0xc", "0x00ff"]
    df["asset"] = ["0123", "77"]  # leading zero must survive
    p = tmp_path / "t.csv"
    df.to_csv(p, index=False)
    st = Store(":memory:")
    import_trades(st, str(p), name="hex", start_ts=0, end_ts=2**40)
    rows = sorted(st.conn.execute("SELECT condition_id, token_id FROM trades").fetchall())
    assert rows == [("0x00ff", "77"), ("0xc", "0123")]
