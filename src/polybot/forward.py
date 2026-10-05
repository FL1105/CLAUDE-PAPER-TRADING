"""Forward (live) paper-trading loop: the only source of admissible evidence.

Each iteration:
    1. ensure the current epoch has a FROZEN wallet set (selected from data before
       the epoch start, leakage self-checked, stored append-only);
    2. poll the public trade feed and pick up trades inserted since the last cursor;
    3. for each BUY by a frozen wallet: fetch the REAL order book at detection (the
       fill reference), then shadow books at +1L, +2L, +5L, then write the fill;
       wallet SELLs are logged as informational; stale detections are skipped;
    4. every ``settle_interval_s``: refresh open markets and settle resolved/void ones;
    5. every ``kill_check_interval_s``: evaluate kill switches; any trigger halts;
    6. heartbeat to the log, the DB and out/heartbeat.json.

Assumptions: public read-only endpoints only (no keys, no orders). State lives in
SQLite, so the loop is resumable: cursors and pending shadow captures are persisted,
and the frozen set for an epoch can never be rewritten.

Failure modes guarded against: silent death (heartbeat), crash mid-signal
(persisted pending queue; on restart, missing shadows are recorded as missing),
copying stale trades after downtime (``max_signal_age_s``), mid-test tuning
(config hash stamped and compared by gate 12).
"""

from __future__ import annotations

import json
import logging
import signal as _signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polybot.config import Config, config_hash
from polybot.data import schemas
from polybot.data.client import ApiError, PolymarketClient
from polybot.data.ingest import Ingestor
from polybot.data.store import Store
from polybot.execution.paper import PaperExecutor, Signal, payouts_for_market
from polybot.gates.ladder import kill_switches
from polybot.report.build import live_evidence
from polybot.selection.walkforward import DataView, Epoch, leakage_selfcheck, select_wallets
from polybot.strategies.base import MarketEvent
from polybot.strategies.wallet_copy import WalletCopy

log = logging.getLogger("polybot.forward")
DAY = 86_400


@dataclass
class Pending:
    signal: Signal
    t_detect: float
    snapshot_id: int | None
    book: dict[str, Any] | None  # serialised BookSnapshot (None = fetch failed / empty)
    shadows: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "signal": self.signal.__dict__,
            "t_detect": self.t_detect,
            "snapshot_id": self.snapshot_id,
            "book": self.book,
            "shadows": self.shadows,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> Pending:
        return Pending(
            Signal(**d["signal"]), d["t_detect"], d["snapshot_id"], d["book"], d["shadows"]
        )


class ForwardLoop:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        client: PolymarketClient,
        out_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg, self.store, self.client, self.out_dir = cfg, store, client, out_dir
        self.clock = clock
        self.hash = config_hash(cfg)
        self.ingest = Ingestor(client, store, cfg)
        self._now = clock()
        self.executor = PaperExecutor(
            store, cfg, evidence_mode="live_forward", config_hash=self.hash, clock=lambda: self._now
        )
        self.strategy = WalletCopy(cfg, cfg.seed)
        self.stop = False
        self._last = {"settle": 0.0, "kill": 0.0, "beat": 0.0}
        self.stats = {"iterations": 0, "signals": 0, "errors": 0}

    def begin(self) -> None:
        """Mark the forward run's start (first time only) and set the trade cursor."""
        st = self.store
        st.set_meta("forward_start_ts", str(int(self.clock())), overwrite=False)
        st.set_meta("forward_config_hash", self.hash, overwrite=False)
        if st.get_meta("forward_config_hash") != self.hash:
            log.warning(
                "CONFIG CHANGED since the forward run began: gate 12 will fail. "
                "Start a new database for a new configuration."
            )
        if st.get_cursor("paper") is None:  # never copy history on first start
            st.set_cursor("paper", {"rowid": st.max_trade_rowid(), "pending": []})

    # --- epochs ------------------------------------------------------------------------
    def current_epoch(self, now: float) -> Epoch:
        fs = int(self.store.get_meta("forward_start_ts") or now)
        step = self.cfg.walkforward.epoch_days * DAY
        k = max(0, int((now - fs) // step))
        return Epoch(k, fs + k * step, fs + (k + 1) * step)

    def ensure_frozen(self, now: float) -> Epoch:
        ep = self.current_epoch(now)
        fz = self.store.frozen_sets()
        row = fz.loc[fz["epoch_start"] == ep.start_ts] if len(fz) else fz
        if len(row):
            self.strategy.set_frozen(frozenset(json.loads(row.iloc[0]["wallets_json"])), ep)
            return ep
        trades, markets = self.store.trades_frame(), self.store.markets_frame()
        sel = select_wallets(
            DataView(trades, markets, ep.start_ts), ep, self.cfg, self.cfg.seed + ep.index
        )
        leaks = (
            leakage_selfcheck(trades, markets, self.cfg, ep, self.cfg.seed + ep.index)
            if len(trades)
            else 0
        )
        diag = json.loads(json.dumps(sel.diagnostics, default=float))
        self.store.save_frozen_set(
            {
                "epoch_start": ep.start_ts,
                "epoch_end": ep.end_ts,
                "wallets_json": json.dumps(list(sel.wallets)),
                "eligible_json": json.dumps({k: list(v) for k, v in sel.eligible.items()}),
                "diagnostics_json": json.dumps(diag),
                "leakage_failures": leaks,
                "config_hash": self.hash,
                "created_ts": int(now),
            }
        )
        if leaks:
            self.store.set_halt("leakage_selfcheck_failed")
        self.strategy.set_frozen(frozenset(sel.wallets), ep)
        log.info(
            "epoch %d frozen: %d wallets (BH), %d eligible",
            ep.index,
            len(sel.wallets),
            len(sel.eligible),
        )
        return ep

    # --- books -------------------------------------------------------------------------
    def fetch_book(self, token: str, now: float) -> tuple[int | None, dict[str, Any] | None]:
        try:
            b = schemas.parse_book(self.client.book(token))
        except (ApiError, schemas.SchemaDriftError) as exc:
            self.store.log_anomaly("book_fetch_error", f"{token}: {exc}")
            return None, None
        sid = self.store.insert_book(b, int(now))
        return sid, b.model_dump(mode="json")

    @staticmethod
    def _book(d: dict[str, Any] | None) -> schemas.BookSnapshot | None:
        return None if d is None else schemas.BookSnapshot.model_validate(d)

    # --- one iteration -------------------------------------------------------------------
    def step(self) -> None:
        if self.store.get_meta("forward_start_ts") is None:
            self.begin()
        now = self.clock()
        self._now = now
        self.ensure_frozen(now)
        cur = self.store.get_cursor("paper") or {"rowid": 0, "pending": []}
        pending = [Pending.from_json(p) for p in cur.get("pending", [])]
        try:
            self.ingest.forward_poll_once()
        except ApiError as exc:
            self.stats["errors"] += 1
            self.store.log_anomaly("poll_error", str(exc))
        new = self.store.trades_after_rowid(int(cur["rowid"]))
        rowid = int(cur["rowid"])
        for r in new.to_dict("records"):
            rowid = max(rowid, int(r["rid"]))
            if r["wallet"] not in self.strategy.frozen:
                continue
            ev = MarketEvent(
                ts=int(r["ts"]),
                wallet=str(r["wallet"]),
                condition_id=str(r["condition_id"] or ""),
                token_id=str(r["token_id"]),
                side=str(r["side"]),
                price=float(r["price"]),
                size_shares=float(r["size_shares"]),
                trade_key=str(r["trade_key"]),
                category=r.get("category"),
            )
            for s in self.strategy.on_event(ev, self.executor.state):
                self.stats["signals"] += 1
                if s.kind == "info_exit":
                    self.executor.handle(s, None)
                elif now - s.ts > self.cfg.paper.max_signal_age_s:
                    self.executor.skip(s, "stale_signal")
                elif not s.condition_id:
                    self.executor.skip(s, "unknown_market")
                else:
                    sid, book = self.fetch_book(s.token_id, now)
                    pending.append(Pending(s, now, sid, book))
        self.store.set_cursor("paper", {"rowid": rowid, "pending": [p.to_json() for p in pending]})
        self.process_pending(pending, now)
        self.periodic(now)

    def process_pending(self, pending: list[Pending], now: float) -> None:
        L = self.cfg.execution.latency_L_s  # noqa: N806
        mults = self.cfg.execution.shadow_multipliers
        still: list[Pending] = []
        for p in pending:
            for m in mults:
                k = str(m)
                if k not in p.shadows and now >= p.t_detect + m * L:
                    late = now > p.t_detect + m * L + max(5 * L, self.cfg.paper.poll_interval_s)
                    p.shadows[k] = None if late else self.fetch_book(p.signal.token_id, now)[1]
            if all(str(m) in p.shadows for m in mults):
                self._now = p.t_detect  # the fill is decided on the detection-time book
                if p.book is None:
                    self.executor.skip(p.signal, "book_fetch_error")
                else:
                    self.executor.handle(
                        p.signal,
                        self._book(p.book),
                        {m: self._book(p.shadows[str(m)]) for m in mults},
                        book_snapshot_id=p.snapshot_id,
                    )
                self._now = now
            else:
                still.append(p)
        cur = self.store.get_cursor("paper") or {"rowid": 0}
        self.store.set_cursor("paper", {**cur, "pending": [p.to_json() for p in still]})
        pending[:] = still

    # --- periodic jobs -----------------------------------------------------------------
    def periodic(self, now: float) -> None:
        pc = self.cfg.paper
        if now - self._last["settle"] >= pc.settle_interval_s:
            self._last["settle"] = now
            self.settle()
        if now - self._last["kill"] >= pc.kill_check_interval_s:
            self._last["kill"] = now
            self.check_kill_switches(now)
        if now - self._last["beat"] >= pc.heartbeat_interval_s:
            self._last["beat"] = now
            self.heartbeat(now)

    def settle(self) -> int:
        open_c = sorted({p.condition_id for p in self.executor.state.open.values()})
        if not open_c:
            return 0
        try:
            self.ingest.fetch_markets(open_c)
        except ApiError as exc:
            self.store.log_anomaly("settle_fetch_error", str(exc))
            return 0
        n = 0
        mk: dict[Any, dict[Any, Any]] = (
            self.store.markets_frame().set_index("market").to_dict("index")
        )
        for cid in open_c:
            if cid not in mk:
                continue
            row = mk[cid]
            if row["status"] not in ("resolved", "void"):
                continue
            toks = dict(
                self.store.conn.execute(
                    "SELECT token_id, outcome_index FROM tokens WHERE condition_id=?", (cid,)
                ).fetchall()
            )
            prices = json.loads(row["settlement_prices"])
            win = (
                None
                if row["winning_outcome"] != row["winning_outcome"]
                else int(row["winning_outcome"])
            )
            rts = None if row["resolved_ts"] != row["resolved_ts"] else int(row["resolved_ts"])
            self._now = float(rts or self.clock())
            n += len(
                self.executor.settle_market(
                    cid,
                    payouts_for_market(str(row["status"]), win, prices, toks),
                    str(row["status"]),
                    rts,
                )
            )
        self._now = self.clock()
        return n

    def check_kill_switches(self, now: float) -> list[str]:
        ev, _ = live_evidence(self.store, self.cfg, int(now), with_placebo=False)
        hit = [k.name for k in kill_switches(ev, self.cfg) if k.triggered]
        if hit and self.store.get_halt() is None:
            self.store.set_halt(",".join(hit))
            log.warning("KILL SWITCH: %s. Halted; review then `polybot resume --note ...`", hit)
        return hit

    def heartbeat(self, now: float) -> None:
        st = self.executor.state
        beat = {
            "ts": int(now),
            "iterations": self.stats["iterations"],
            "signals": self.stats["signals"],
            "errors": self.stats["errors"],
            "open_positions": len(st.open),
            "cash": round(st.cash, 4),
            "realized_pnl": round(st.realized_pnl, 4),
            "halted": self.store.get_halt(),
            "frozen_wallets": len(self.strategy.frozen),
            "config_hash": self.hash,
        }
        self.store.set_meta("heartbeat", json.dumps(beat))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "heartbeat.json").write_text(json.dumps(beat, indent=1))
        log.info("heartbeat %s", beat)

    # --- run -----------------------------------------------------------------------------
    def run(
        self, *, max_iterations: int | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        def _stop(*_: object) -> None:
            self.stop = True
            log.info("stop requested; finishing the current iteration")

        try:
            _signal.signal(_signal.SIGINT, _stop)
            _signal.signal(_signal.SIGTERM, _stop)
        except ValueError:  # not in the main thread (tests)
            pass
        self.begin()
        while not self.stop:
            t0 = self.clock()
            try:
                self.step()
            except Exception as exc:  # logged, counted, surfaced in the report; never silent
                self.stats["errors"] += 1
                self.store.log_anomaly("loop_error", repr(exc))
                log.exception("iteration failed; continuing")
            self.stats["iterations"] += 1
            if max_iterations is not None and self.stats["iterations"] >= max_iterations:
                break
            sleep(max(0.0, self.cfg.paper.poll_interval_s - (self.clock() - t0)))
        self.heartbeat(self.clock())
