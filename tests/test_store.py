from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from polybot.data import schemas
from polybot.data.store import Store
from tests.conftest import load_fixture


@pytest.fixture
def st() -> Store:
    return Store(":memory:")


def _markets() -> list:
    return [schemas.parse_gamma_market(m) for m in load_fixture("gamma_markets_closed")]


def _trades() -> list[schemas.TradeRecord]:
    return [schemas.parse_trade(t) for t in load_fixture("data_trades")]


def test_upsert_markets_idempotent_and_updates_resolution(st: Store) -> None:
    open_m = schemas.parse_gamma_market(load_fixture("gamma_markets_open")[0])
    assert st.upsert_markets([open_m]) == 1
    st.upsert_markets([open_m])
    assert st.count("markets") == 1 and st.count("tokens") == 2
    raw = dict(
        load_fixture("gamma_markets_open")[0],
        closed=True,
        outcomePrices='["0","1"]',
        closedTime="2026-11-02T00:00:00Z",
    )
    st.upsert_markets([schemas.parse_gamma_market(raw)])
    row = st.conn.execute("SELECT resolution_status, winning_outcome_index FROM markets").fetchone()
    assert row == ("resolved", 1)


def test_insert_trades_idempotent(st: Store) -> None:
    assert st.insert_trades(_trades(), "test") == 2
    assert st.insert_trades(_trades(), "test") == 0
    assert st.count("trades") == 2


def test_backfill_trade_links(st: Store) -> None:
    t = _trades()[0].model_copy(update={"condition_id": "", "outcome_index": None})
    st.insert_trades([t], "import:x")
    assert st.trades_frame().empty  # not analysable until linked
    st.upsert_markets([schemas.parse_gamma_market(load_fixture("gamma_markets_open")[0])])
    assert st.backfill_trade_links() == 1
    f = st.trades_frame()
    assert len(f) == 1 and f.outcome.iloc[0] == 0


def test_frames_hide_winner_unless_resolved(st: Store) -> None:
    st.upsert_markets(_markets())
    m = st.markets_frame().set_index("status")
    assert m.loc["resolved", "winning_outcome"] == 0
    assert np.isnan(m.loc["void", "winning_outcome"])


def test_paper_tables_are_append_only(st: Store) -> None:
    c = st.conn
    c.execute("""INSERT INTO paper_signals(created_ts, strategy, wallet, condition_id, token_id,
                 wallet_trade_key, wallet_price, status, evidence_mode, config_hash)
                 VALUES (1,'s','w','c','t','k',0.5,'skipped','live_forward','h')""")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE paper_signals SET status='filled'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM paper_signals")


def test_cursor_anomaly_drift_book(st: Store) -> None:
    assert st.get_cursor("j") is None
    st.set_cursor("j", {"offset": 5})
    st.set_cursor("j", {"offset": 10})
    assert st.get_cursor("j") == {"offset": 10}
    st.log_anomaly("x", "detail")
    st.log_drift("data/trades", "bad")
    assert st.anomaly_counts() == {"x": 1} and st.drift_count_since(0) == 1
    b = schemas.parse_book(load_fixture("clob_book"))
    sid = st.insert_book(b, 123)
    assert st.insert_book(b, 123) == sid
    with pytest.raises(ValueError):
        st.count("sqlite_master")


def test_file_store_persists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "db" / "x.sqlite"
    with Store(p) as s:
        s.insert_trades(_trades(), "t")
    with Store(p) as s:
        assert s.count("trades") == 2
