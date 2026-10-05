from __future__ import annotations

import json
import sqlite3

import pytest

from polybot.config import Config
from polybot.data.schemas import BookSnapshot, Level
from polybot.data.store import Store
from polybot.execution.paper import PaperExecutor, Signal, payouts_for_market


def book(*levels: tuple[float, float], ts: int = 100) -> BookSnapshot:
    return BookSnapshot(
        token_id="t1",
        ts=ts,
        bids=(),
        tick_size=0.01,
        min_order_size=None,
        asks=tuple(Level(price=p, size=s) for p, s in levels),
    )


DEEP = book((0.50, 1000), (0.51, 1000))


def sig(
    key: str = "k1", price: float | None = 0.49, cid: str = "c1", tok: str = "t1", **kw: object
) -> Signal:
    return Signal(
        strategy="wallet_copy",
        condition_id=cid,
        token_id=tok,
        ts=1,
        wallet="w",
        wallet_price=price,
        wallet_trade_key=key,
        **kw,
    )  # type: ignore[arg-type]


@pytest.fixture
def ex(cfg: Config) -> PaperExecutor:
    return PaperExecutor(
        Store(":memory:"), cfg, evidence_mode="synthetic", config_hash="h", clock=lambda: 1000.0
    )


def test_fill_records_everything(ex: PaperExecutor) -> None:
    shadows = {1: book((0.52, 1000)), 2: book((0.55, 1000)), 5: None}
    o = ex.handle(sig(), DEEP, shadows, book_snapshot_id=None)
    assert o.status == "filled" and o.fill is not None
    stake = 0.005 * 1000
    assert o.fill.cost == pytest.approx(stake) and o.fill.avg_price == 0.5
    assert o.fee == pytest.approx(0.05 * 10 * 0.5 * 0.5)
    f = ex.store.fills_frame().iloc[0]
    assert f.slippage == pytest.approx(0.01) and f.slippage_vs_best == 0
    assert f.config_hash == "h" and f.evidence_mode == "synthetic"
    sh = json.loads(f.shadow_fills_json)
    assert sh["1"]["avg_price"] == 0.52 and sh["2"]["avg_price"] == 0.55
    assert sh["5"] == {"status": "missing"}
    assert ex.state.cash == pytest.approx(1000 - stake - o.fee)
    assert len(ex.state.open) == 1


def test_duplicate_signal_ignored(ex: PaperExecutor) -> None:
    ex.handle(sig(), DEEP)
    assert ex.handle(sig(), DEEP).status == "duplicate"
    assert len(ex.store.signals_frame()) == 1


def test_chase_rule_skips_and_logs_reason(ex: PaperExecutor) -> None:
    o = ex.handle(sig("a", price=0.40), DEEP)  # best ask 0.50 > 0.40 + 0.03
    assert o.status == "skipped" and o.reason == "chase:best_ask_beyond_max_chase"
    thin = book((0.50, 2), (0.60, 1000))  # $1 within chase band < $5 stake
    o2 = ex.handle(sig("b"), thin)
    assert o2.reason == "chase:insufficient_depth_within_max_chase"
    o3 = ex.handle(sig("c"), book())
    assert o3.reason == "empty_book"
    o4 = ex.handle(sig("d"), None)
    assert o4.reason == "empty_book"
    s = ex.store.signals_frame()
    assert list(s.status) == ["skipped"] * 4 and s.skip_reason.notna().all()


def test_no_wallet_price_has_no_chase_cap(ex: PaperExecutor) -> None:
    o = ex.handle(sig("x", price=None), DEEP)
    assert o.status == "filled"
    assert ex.store.fills_frame().iloc[0].slippage is None


def test_risk_limits(cfg: Config) -> None:
    pc = cfg.paper.model_copy(update={"max_concurrent_positions": 2})
    ex = PaperExecutor(
        Store(":memory:"),
        cfg.model_copy(update={"paper": pc}),
        evidence_mode="synthetic",
        config_hash="h",
    )
    # 2% per market = $20 -> 4 stakes of $5 in one market, 5th refused
    reasons = [ex.handle(sig(f"m{i}", cid="c1"), DEEP).reason for i in range(5)]
    assert reasons[:2] == ["", ""] and reasons[2] == "risk:max_concurrent_positions"


def test_per_market_and_exposure_limits(cfg: Config) -> None:
    ex = PaperExecutor(Store(":memory:"), cfg, evidence_mode="synthetic", config_hash="h")
    r = [ex.handle(sig(f"m{i}", cid="c1"), DEEP).reason for i in range(5)]
    assert r == ["", "", "", "", "risk:max_per_market"]
    pc = cfg.paper.model_copy(update={"max_total_exposure_frac": 0.01})
    ex2 = PaperExecutor(
        Store(":memory:"),
        cfg.model_copy(update={"paper": pc}),
        evidence_mode="synthetic",
        config_hash="h",
    )
    r2 = [ex2.handle(sig(f"n{i}", cid=f"c{i}"), DEEP).reason for i in range(3)]
    assert r2 == ["", "", "risk:max_total_exposure"]


def test_insufficient_cash(cfg: Config) -> None:
    pc = cfg.paper.model_copy(
        update={"stake_frac": 0.5, "max_market_frac": 1.0, "max_total_exposure_frac": 1.0}
    )
    ex = PaperExecutor(
        Store(":memory:"),
        cfg.model_copy(update={"paper": pc}),
        evidence_mode="synthetic",
        config_hash="h",
    )
    deep = book((0.5, 10**6))
    assert ex.handle(sig("a", cid="a"), deep).status == "filled"
    assert ex.handle(sig("b", cid="b"), deep).reason == "risk:insufficient_cash"


def test_wallet_exit_is_informational(ex: PaperExecutor) -> None:
    o = ex.handle(sig("e", kind="info_exit"), DEEP)
    assert o.status == "info" and not ex.state.open
    assert ex.store.signals_frame().iloc[0].skip_reason == "wallet_exit_not_mirrored"


def test_settlement_win_loss_void_and_crash_recovery(cfg: Config, tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "p.sqlite"
    ex = PaperExecutor(Store(db), cfg, evidence_mode="backtest", config_hash="h")
    a = ex.handle(sig("a", cid="win", tok="ty"), book((0.5, 1000)))
    b = ex.handle(sig("b", cid="lose", tok="tn"), book((0.5, 1000)))
    c = ex.handle(sig("c", cid="void", tok="tv"), book((0.5, 1000)))
    assert a.fill and b.fill and c.fill
    ex.settle_market(
        "win", payouts_for_market("resolved", 0, [1, 0], {"ty": 0, "x": 1}), "resolved", 50
    )
    ex.settle_market(
        "lose", payouts_for_market("resolved", 1, [0, 1], {"tn": 0, "y": 1}), "resolved", 60
    )
    s = ex.store.settlements_frame()
    assert s.net_pnl.iloc[0] == pytest.approx(10 - 5 - a.fee)
    assert s.net_pnl.iloc[1] == pytest.approx(-5 - b.fee)
    cash_before = ex.state.cash

    ex2 = PaperExecutor(Store(db), cfg, evidence_mode="backtest", config_hash="h")  # "crash"
    assert ex2.state.cash == pytest.approx(cash_before)
    assert list(ex2.state.open) == [c.fill_id]
    ex2.settle_market("void", payouts_for_market("void", None, [0.5, 0.5], {"tv": 0}), "void", 70)
    assert ex2.store.settlements_frame().net_pnl.iloc[2] == pytest.approx(-c.fee)  # refund
    assert ex2.state.realized_pnl == pytest.approx(5 - a.fee - 5 - b.fee - c.fee)
    assert ex2.state.settled_equity == pytest.approx(1000 + ex2.state.realized_pnl)


def test_settlement_guards(ex: PaperExecutor) -> None:
    ex.handle(sig(), DEEP)
    with pytest.raises(ValueError):
        ex.settle_market("c1", {"t1": 1.0}, "open", None)
    with pytest.raises(KeyError):
        ex.settle_market("c1", {"other": 1.0}, "resolved", 1)
    with pytest.raises(ValueError):
        ex.settle_market("c1", {"t1": 2.0}, "resolved", 1)
    with pytest.raises(ValueError):
        payouts_for_market("resolved", None, [], {})
    with pytest.raises(ValueError):
        payouts_for_market("ambiguous", None, [], {})


def test_ledger_rows_cannot_be_edited(ex: PaperExecutor) -> None:
    ex.handle(sig(), DEEP)
    with pytest.raises(sqlite3.DatabaseError):
        ex.store.conn.execute("UPDATE paper_fills SET fill_price = 0.01")


def test_fee_stress_multiplier(ex: PaperExecutor) -> None:
    o1 = ex.handle(sig("a", cid="a"), DEEP)
    o2 = ex.handle(sig("b", cid="b"), DEEP, fee_multiplier=2.0)
    assert o2.fee == pytest.approx(2 * o1.fee)
