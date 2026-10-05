from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from polybot.config import Config
from polybot.data.client import HttpClient, PolymarketClient
from polybot.data.ingest import Ingestor
from polybot.data.store import Store
from tests.conftest import load_fixture

BASE_TS = 1_759_600_000


def feed(n: int) -> list[dict[str, Any]]:
    """n trades, newest first, one per minute."""
    proto = load_fixture("data_trades")[0]
    out = []
    for i in range(n):
        t = copy.deepcopy(proto)
        t["timestamp"] = BASE_TS - i * 60
        t["transactionHash"] = f"0x{i:064x}"
        out.append(t)
    return out


class FakeApi:
    def __init__(self, trades: list[dict[str, Any]], cap_limit: int = 50) -> None:
        self.trades = trades
        self.cap_limit = cap_limit
        self.calls: list[httpx.URL] = []
        self.markets = load_fixture("gamma_markets_closed") + load_fixture("gamma_markets_open")

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url)
        p = req.url.params
        if req.url.path in ("/trades", "/activity"):
            lim = min(int(p.get("limit", 100)), self.cap_limit)
            off = int(p.get("offset", 0))
            rows = self.trades
            if "start" in p:
                rows = [r for r in rows if int(p["start"]) <= r["timestamp"] < int(p["end"])]
            return httpx.Response(200, json=rows[off : off + lim])
        if req.url.path == "/markets":
            cids = p.get_list("condition_ids")
            toks = p.get_list("clob_token_ids")
            hit = [
                m
                for m in self.markets
                if m["conditionId"] in cids or any(t in m["clobTokenIds"] for t in toks)
            ]
            return httpx.Response(200, json=hit)
        return httpx.Response(404)


def make(cfg: Config, api: FakeApi, **ingest: Any) -> tuple[Ingestor, Store]:
    c = cfg.model_copy(
        update={"ingest": cfg.ingest.model_copy(update={"page_limit": 50, **ingest})}
    )
    http = HttpClient(c.api, transport=httpx.MockTransport(api), sleep=lambda s: None)
    st = Store(":memory:")
    return Ingestor(PolymarketClient(http), st, c), st


def test_backfill_stops_at_start(cfg: Config) -> None:
    ing, st = make(cfg, FakeApi(feed(300)), max_offset=10_000)
    r = ing.backfill_trades(BASE_TS - 100 * 60, BASE_TS + 1)
    assert r.stopped_because == "reached_start"
    assert st.count("trades") == 101  # minutes 0..100 inclusive


def test_backfill_offset_cap_is_logged_not_hidden(cfg: Config) -> None:
    ing, st = make(cfg, FakeApi(feed(300)), max_offset=100)
    r = ing.backfill_trades(BASE_TS - 10**6, BASE_TS + 1)
    assert r.stopped_because == "offset_cap"
    assert st.anomaly_counts() == {"history_cap_reached": 1}
    assert r.oldest_ts == BASE_TS - 149 * 60  # what we really reached


def test_backfill_resumes_from_cursor(cfg: Config) -> None:
    api = FakeApi(feed(200))
    ing, st = make(cfg, api, max_offset=60)
    ing.backfill_trades(BASE_TS - 10**6, BASE_TS + 1)  # stops at offset cap (100)
    n_calls = len(api.calls)
    ing.cfg = ing.cfg.model_copy(
        update={"ingest": ing.cfg.ingest.model_copy(update={"max_offset": 10_000})}
    )
    ing.backfill_trades(BASE_TS - 10**6, BASE_TS + 1)
    first_resumed = api.calls[n_calls]
    assert first_resumed.params["offset"] == "100"
    assert st.count("trades") == 200


def test_forward_poll_catches_up_and_is_idempotent(cfg: Config) -> None:
    api = FakeApi(feed(120))
    ing, st = make(cfg, api, max_offset=10_000, forward_overlap_pages=0)
    r1 = ing.forward_poll_once()
    assert st.count("trades") == 120 and r1.stopped_because == "empty_page"
    r2 = ing.forward_poll_once()
    assert r2.inserted == 0 and r2.stopped_because == "caught_up" and r2.pages == 1


def test_forward_poll_gap_logged(cfg: Config) -> None:
    ing, st = make(cfg, FakeApi(feed(300)), max_offset=60)
    r = ing.forward_poll_once()
    assert r.stopped_because == "offset_cap"
    assert "forward_gap_possible" in st.anomaly_counts()


def test_drift_rows_counted_not_guessed(cfg: Config) -> None:
    rows = feed(5)
    rows[2] = {k: v for k, v in rows[2].items() if k != "proxyWallet"}
    ing, st = make(cfg, FakeApi(rows), max_offset=10_000)
    r = ing.forward_poll_once()
    assert r.drift == 1 and st.count("trades") == 4 and st.count("schema_drift") == 1


def test_wallet_history_window_and_type_filter(cfg: Config) -> None:
    rows = feed(10)
    rows[0]["type"] = "REDEEM"
    ing, st = make(cfg, FakeApi(rows), max_offset=10_000)
    r = ing.backfill_wallet("0xabc", BASE_TS - 5 * 60, BASE_TS + 1)
    assert st.count("trades") == 5 and r.stopped_because == "empty_page"


def test_refresh_markets_by_condition_and_token(cfg: Config) -> None:
    ing, st = make(cfg, FakeApi(feed(3)), max_offset=10_000)
    ing.forward_poll_once()  # trades reference the open fixture market
    from polybot.data import schemas

    orphan = schemas.parse_trade(
        dict(
            load_fixture("data_trades")[0],
            conditionId="0xmissing",
            asset="1111111111111111111111",
            transactionHash="0x9",
        )
    )
    orphan2 = orphan.model_copy(
        update={"condition_id": "", "trade_key": "k2", "outcome_index": None}
    )
    st.insert_trades([orphan, orphan2], "import:x")
    r = ing.refresh_markets()
    assert st.count("markets") == 2  # open market + one found via token id
    assert st.anomaly_counts().get("market_not_found") == 1  # 0xmissing
    assert r.inserted >= 2
    f = st.trades_frame()
    assert (f.market == load_fixture("gamma_markets_closed")[0]["conditionId"]).sum() == 1


def test_bad_lookup_key(cfg: Config) -> None:
    ing, _ = make(cfg, FakeApi([]))
    with pytest.raises(ValueError):
        ing.fetch_markets(["x"], by="slug")
