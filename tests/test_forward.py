"""End-to-end forward loop against a fake public API (no network, fake clock)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from polybot.config import Config
from polybot.data.client import HttpClient, PolymarketClient
from polybot.data.store import Store
from polybot.forward import ForwardLoop
from polybot.report.build import write_outputs

W = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
C = "0x" + "c" * 64
T0 = 1_800_000_000


class Clock:
    def __init__(self) -> None:
        self.t = float(T0)

    def __call__(self) -> float:
        return self.t


class FakeApi:
    def __init__(self) -> None:
        self.feed: list[dict[str, Any]] = []
        self.closed = False
        self.book_calls = 0

    def trade(self, wallet: str, side: str, price: float, ts: int, tx: str) -> None:
        self.feed.insert(
            0,
            {
                "proxyWallet": wallet,
                "side": side,
                "asset": "TOK0",
                "conditionId": C,
                "size": 20.0,
                "price": price,
                "timestamp": ts,
                "outcomeIndex": 0,
                "transactionHash": tx,
            },
        )

    def market(self) -> dict[str, Any]:
        return {
            "conditionId": C,
            "question": "fake?",
            "endDate": "2027-01-01T00:00:00Z",
            "closed": self.closed,
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["1","0"]' if self.closed else '["0.5","0.5"]',
            "clobTokenIds": '["TOK0","TOK1"]',
            "closedTime": "2027-01-02T00:00:00Z" if self.closed else None,
        }

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.params
        if req.url.path == "/trades":
            off, lim = int(p.get("offset", 0)), int(p.get("limit", 100))
            return httpx.Response(200, json=self.feed[off : off + lim])
        if req.url.path == "/book":
            self.book_calls += 1
            px = 0.51 + 0.01 * (self.book_calls - 1)  # price drifts up with each fetch
            return httpx.Response(
                200,
                json={
                    "asset_id": p["token_id"],
                    "timestamp": "1",
                    "bids": [],
                    "asks": [{"price": str(round(px, 2)), "size": "1000"}],
                    "tick_size": "0.01",
                },
            )
        if req.url.path == "/markets":
            return httpx.Response(200, json=[self.market()])
        return httpx.Response(404)


@pytest.fixture
def env(cfg: Config, tmp_path: Path):  # type: ignore[no-untyped-def]
    api, clock = FakeApi(), Clock()
    pc = cfg.paper.model_copy(
        update={"settle_interval_s": 60, "kill_check_interval_s": 60, "heartbeat_interval_s": 1}
    )
    c = cfg.model_copy(update={"paper": pc})
    db = tmp_path / "f.sqlite"

    def make() -> ForwardLoop:
        http = HttpClient(c.api, transport=httpx.MockTransport(api), sleep=lambda s: None)
        return ForwardLoop(c, Store(db), PolymarketClient(http), tmp_path / "out", clock=clock)

    loop = make()
    clock.t = T0 - 5000  # the forward run began a while ago ...
    loop.begin()
    loop.ingest.fetch_markets([C])  # market metadata known up front
    loop.store.save_frozen_set(
        {
            "epoch_start": int(T0) - 5000,
            "epoch_end": int(T0) - 5000 + 14 * 86_400,
            "wallets_json": json.dumps([W]),
            "eligible_json": json.dumps({W: [40, 0.5]}),
            "diagnostics_json": json.dumps({"cap": 100.0}),
            "leakage_failures": 0,
            "config_hash": loop.hash,
            "created_ts": int(T0),
        }
    )
    clock.t = T0  # ... and it is now T0
    return api, clock, loop, make, c, tmp_path


def test_full_forward_cycle(env) -> None:  # type: ignore[no-untyped-def]
    api, clock, loop, make, c, tmp = env
    api.trade(W, "BUY", 0.50, T0 - 1000, "0x1")  # stale by the time we see it
    api.trade(OTHER, "BUY", 0.50, T0 - 1, "0x2")  # not a frozen wallet
    api.trade(W, "BUY", 0.50, T0 - 1, "0x3")  # fresh: copy it
    loop.step()
    sig = loop.store.signals_frame()
    assert list(sig.skip_reason) == ["stale_signal"]
    assert len(loop.store.get_cursor("paper")["pending"]) == 1  # waiting for shadow books

    for dt in (2, 4, 10):  # 1xL, 2xL, 5xL with L = 2s
        clock.t = T0 + dt
        loop.step()
    f = loop.store.fills_frame()
    assert len(f) == 1 and f.iloc[0].fill_price == 0.51  # detection-time book, not later ones
    assert f.iloc[0].evidence_mode == "live_forward" and f.iloc[0].created_ts == T0
    sh = json.loads(f.iloc[0].shadow_fills_json)
    assert [sh[k]["avg_price"] for k in ("1", "2", "5")] == [0.52, 0.53, 0.54]
    assert loop.store.get_cursor("paper")["pending"] == []

    api.trade(W, "SELL", 0.6, int(clock.t), "0x4")
    loop.step()
    assert loop.store.signals_frame().status.tolist()[-1] == "info"

    api.closed = True
    clock.t = T0 + 120  # past settle interval
    loop.step()
    s = loop.store.settlements_frame()
    assert len(s) == 1 and s.iloc[0].payout == pytest.approx(f.iloc[0].shares)
    assert loop.executor.state.realized_pnl > 0
    assert (tmp / "out" / "heartbeat.json").exists()

    # restart: state rebuilt from the ledger, nothing processed twice
    loop2 = make()
    loop2.step()
    assert len(loop2.store.signals_frame()) == 3 and len(loop2.store.fills_frame()) == 1
    assert loop2.executor.state.realized_pnl == pytest.approx(loop.executor.state.realized_pnl)

    rep = write_outputs(loop2.store, c, tmp / "rep", now=int(T0 + 200))
    assert rep["summary"]["settled_trades"] == 1 and rep["ladder"]["tier"] is None
    assert (tmp / "rep" / "trades.csv").read_text(encoding="utf-8").count(
        "\n"
    ) == 4  # header + 3 signals
    assert rep["latency_curve"] and rep["equity"]


def test_crash_with_pending_signal_recovers(env) -> None:  # type: ignore[no-untyped-def]
    api, clock, loop, make, _, _ = env
    api.trade(W, "BUY", 0.50, T0 - 1, "0x9")
    loop.step()  # book fetched at detection, shadows pending ... then "crash"
    clock.t = T0 + 600  # comes back much later: shadows are too late to be honest
    loop2 = make()
    loop2.step()
    f = loop2.store.fills_frame()
    assert len(f) == 1
    sh = json.loads(f.iloc[0].shadow_fills_json)
    assert all(v == {"status": "missing"} for v in sh.values())


def test_kill_switch_halts_new_entries(env) -> None:  # type: ignore[no-untyped-def]
    api, clock, loop, _, _, _ = env
    loop.store.set_halt("manual_test")
    api.trade(W, "BUY", 0.50, T0 - 1, "0x7")
    for dt in (0, 2, 4, 10):
        clock.t = T0 + dt
        loop.step()
    assert loop.store.signals_frame().skip_reason.tolist() == ["halted:manual_test"]


def test_ensure_frozen_selects_from_history_once(cfg: Config, tmp_path: Path) -> None:
    api, clock = FakeApi(), Clock()
    http = HttpClient(cfg.api, transport=httpx.MockTransport(api), sleep=lambda s: None)
    loop = ForwardLoop(
        cfg, Store(tmp_path / "x.sqlite"), PolymarketClient(http), tmp_path / "o", clock=clock
    )
    loop.begin()
    ep = loop.ensure_frozen(clock())
    assert ep.start_ts == T0 and len(loop.store.frozen_sets()) == 1
    loop.ensure_frozen(clock() + 100)  # same epoch: reused, not re-selected
    assert len(loop.store.frozen_sets()) == 1
    clock.t = T0 + 15 * 86_400
    assert loop.ensure_frozen(clock()).index == 1 and len(loop.store.frozen_sets()) == 2


def test_run_loop_and_errors_are_counted(env) -> None:  # type: ignore[no-untyped-def]
    _, _, loop, _, _, _ = env

    def boom() -> None:
        raise RuntimeError("x")

    loop.step = boom  # type: ignore[method-assign]
    loop.run(max_iterations=2, sleep=lambda s: None)
    assert loop.stats["errors"] == 2
    assert loop.store.anomaly_counts()["loop_error"] == 2


# --- regression tests for review findings -------------------------------------------


def test_drift_does_not_rehalt_after_resume(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import polybot.data.store as store_mod

    _, clock, loop, _, _, _ = env
    monkeypatch.setattr(store_mod.time, "time", clock)  # the DB stamps rows with the test clock
    loop.store.log_drift("data/trades", "one malformed row")
    assert "data_integrity" in loop.check_kill_switches(clock())
    assert loop.store.get_halt() is not None
    clock.t += 10
    loop.store.clear_halt("reviewed: single malformed feed row")
    clock.t += 10
    # the old drift row is before the clearance: no immediate re-halt
    assert "data_integrity" not in loop.check_kill_switches(clock())
    assert loop.store.get_halt() is None


def test_slippage_kill_switch_fires_from_live_fills(env) -> None:  # type: ignore[no-untyped-def]
    api, clock, loop, _, _, _ = env
    for d in range(3):  # three days of fills at 0.53 vs wallet 0.50 = 3c slippage (> 2 x 1c)
        api.book_calls = 2  # book quote at 0.53
        tx = f"0xs{d}"
        api.trade(W, "BUY", 0.50, int(clock.t) - 1, tx)
        base = clock.t
        for dt in (0, 2, 4, 10):
            clock.t = base + dt
            loop.step()
        clock.t = base + 86_400
    fills = loop.store.fills_frame()
    assert len(fills) == 3 and (fills.slippage > 0.02).all()
    assert "slippage" in loop.check_kill_switches(clock())


def test_bad_poll_payload_does_not_stall_the_loop(env) -> None:  # type: ignore[no-untyped-def]
    api, clock, loop, _, _, _ = env
    api.trade(W, "BUY", 0.50, T0 - 1, "0xa")
    loop.step()  # pending signal with its detection book
    original = api.__call__

    def broken(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/trades":
            return httpx.Response(200, json={"error": "maintenance"})
        return original(req)

    loop.client.http._http._transport = httpx.MockTransport(broken)
    for dt in (2, 4, 10):
        clock.t = T0 + dt
        loop.step()  # must not raise; shadows still captured, fill written
    assert len(loop.store.fills_frame()) == 1
    assert loop.store.anomaly_counts().get("poll_error", 0) >= 1


def test_stale_signals_do_not_count_against_fill_rate(env) -> None:  # type: ignore[no-untyped-def]
    from polybot.report.build import live_evidence

    api, clock, loop, _, c, _ = env
    api.trade(W, "BUY", 0.50, T0 - 1000, "0xold")  # stale (PC was asleep)
    api.trade(W, "BUY", 0.50, T0 - 1, "0xnew")
    for dt in (0, 2, 4, 10):
        clock.t = T0 + dt
        loop.step()
    ev, _ = live_evidence(loop.store, c, int(clock.t), with_placebo=False)
    assert ev.signals_reached_book == 1 and ev.signals_filled == 1  # 100%, not 50%
