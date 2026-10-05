from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.data import schemas
from polybot.data.schemas import ResolutionStatus, SchemaDriftError
from tests.conftest import LIVE, load_fixture


def test_parse_ts_variants() -> None:
    assert schemas.parse_ts(1759600000) == 1759600000
    assert schemas.parse_ts("1759600001234") == 1759600001  # ms string
    assert schemas.parse_ts("2025-12-31T12:00:00Z") == 1767182400
    assert schemas.parse_ts("2026-01-01 03:12:45+00") == 1767237165
    assert schemas.parse_ts("2025-12-31T12:00:00") == 1767182400  # naive -> UTC
    assert schemas.parse_ts(None) is None and schemas.parse_ts("") is None
    with pytest.raises(ValueError):
        schemas.parse_ts(True)


def test_resolved_market_and_tokens() -> None:
    m, toks = schemas.parse_gamma_market(load_fixture("gamma_markets_closed")[0])
    assert m.resolution_status is ResolutionStatus.RESOLVED
    assert m.winning_outcome_index == 0
    assert m.resolved_ts == 1767237165 and m.end_ts == 1767182400
    assert [t.outcome_label for t in toks] == ["Yes", "No"]
    assert toks[1].token_id == "2222222222222222222222" and toks[1].outcome_index == 1


def test_void_market_is_not_given_a_winner() -> None:
    m, _ = schemas.parse_gamma_market(load_fixture("gamma_markets_closed")[1])
    assert m.resolution_status is ResolutionStatus.VOID
    assert m.winning_outcome_index is None
    assert m.settlement_prices == (0.5, 0.5)


def test_open_market_never_resolved_from_prices() -> None:
    m, _ = schemas.parse_gamma_market(load_fixture("gamma_markets_open")[0])
    assert m.resolution_status is ResolutionStatus.OPEN
    assert m.winning_outcome_index is None and m.resolved_ts is None


def test_closed_market_with_unclean_prices_is_ambiguous() -> None:
    raw = dict(load_fixture("gamma_markets_closed")[0], outcomePrices='["0.97","0.03"]')
    m, _ = schemas.parse_gamma_market(raw)
    # never infer the winner from a last trade/near-final price
    assert m.resolution_status is ResolutionStatus.AMBIGUOUS
    assert m.winning_outcome_index is None


def test_gamma_accepts_real_lists_as_well_as_json_strings() -> None:
    raw = dict(load_fixture("gamma_markets_closed")[0])
    raw["outcomes"] = ["Yes", "No"]
    raw["outcomePrices"] = ["1", "0"]
    raw["clobTokenIds"] = ["a", "b"]
    m, toks = schemas.parse_gamma_market(raw)
    assert m.winning_outcome_index == 0 and [t.token_id for t in toks] == ["a", "b"]


def test_missing_required_field_is_drift_and_counted() -> None:
    raw = dict(load_fixture("gamma_markets_closed")[0])
    del raw["conditionId"]
    before = schemas.DRIFT_COUNTER["gamma/markets"]
    with pytest.raises(SchemaDriftError):
        schemas.parse_gamma_market(raw)
    assert schemas.DRIFT_COUNTER["gamma/markets"] == before + 1


def test_outcome_token_count_mismatch_is_drift() -> None:
    raw = dict(load_fixture("gamma_markets_closed")[0], clobTokenIds='["only-one"]')
    with pytest.raises(SchemaDriftError):
        schemas.parse_gamma_market(raw)


def test_trade_parsing() -> None:
    t = schemas.parse_trade(load_fixture("data_trades")[0])
    assert t.side == "BUY" and t.price == 0.61 and t.size_shares == 100.0
    assert t.size_usdc == pytest.approx(61.0)
    assert t.wallet == t.wallet.lower() and len(t.trade_key) == 64


def test_trade_key_distinguishes_fills_in_same_tx() -> None:
    a = load_fixture("data_trades")[0]
    b = dict(a, size=50.0)  # same tx hash, different fill
    assert schemas.parse_trade(a).trade_key != schemas.parse_trade(b).trade_key
    assert schemas.parse_trade(a).trade_key == schemas.parse_trade(dict(a)).trade_key


@pytest.mark.parametrize(
    "bad",
    [
        {"side": "HOLD"},
        {"price": 1.5},
        {"timestamp": None},
        {"proxyWallet": ""},
        {"conditionId": None},
    ],
)
def test_bad_trades_are_drift(bad: dict) -> None:
    with pytest.raises(SchemaDriftError):
        schemas.parse_trade(dict(load_fixture("data_trades")[0], **bad))


def test_book_sorted_explicitly_and_ms_timestamp() -> None:
    b = schemas.parse_book(load_fixture("clob_book"))
    assert [lv.price for lv in b.asks] == [0.62, 0.63, 0.66]
    assert [lv.price for lv in b.bids] == [0.61, 0.60, 0.58]
    assert b.best_ask == 0.62 and b.best_bid == 0.61
    assert b.ts == 1759600001 and b.tick_size == 0.01


def test_empty_book() -> None:
    b = schemas.parse_book({"asset_id": "x", "timestamp": "1", "bids": [], "asks": []})
    assert b.best_ask is None and b.best_bid is None


def test_book_drops_zero_size_levels() -> None:
    raw = {
        "asset_id": "x",
        "timestamp": 5,
        "bids": [],
        "asks": [{"price": "0.5", "size": "0"}, {"price": "0.6", "size": "3"}],
    }
    assert schemas.parse_book(raw).best_ask == 0.6


# --- once `polybot probe` has run, validate the adapters against REAL responses ---------

PARSERS = {
    "gamma_markets_closed": schemas.parse_gamma_market,
    "gamma_markets_open": schemas.parse_gamma_market,
    "data_trades_global": schemas.parse_trade,
    "data_trades_user": schemas.parse_trade,
    "clob_book": schemas.parse_book,
}
LIVE_FILES = sorted(p for p in LIVE.glob("*.json") if p.stem in PARSERS) if LIVE.exists() else []


@pytest.mark.skipif(not LIVE_FILES, reason="no live fixtures yet: run `polybot probe`")
@pytest.mark.parametrize("path", LIVE_FILES, ids=lambda p: p.stem)
def test_adapters_accept_live_fixtures(path: Path) -> None:
    payload = json.loads(path.read_text())
    items = payload if isinstance(payload, list) else [payload]
    assert items, f"{path.name} is empty"
    for it in items:
        PARSERS[path.stem](it)
