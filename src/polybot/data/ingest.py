"""Ingestion from the public API: historical backfill, forward poll, resolutions.

Assumptions (UNVERIFIED until `polybot probe`; see docs/ASSUMPTIONS.md):
    * Data API ``/trades`` is newest-first with ``limit``/``offset`` pagination,
      capped at ``ingest.max_offset``. The global feed therefore cannot reach deep
      history; we record the oldest timestamp actually reached instead of
      pretending.
    * Data API ``/activity?user=`` accepts ``start``/``end`` (epoch s) and returns
      TRADE rows with the same fields as ``/trades``; this is the per-wallet route
      to deeper history.
    * Gamma ``/markets`` accepts repeated ``condition_ids`` to look markets up.

Failure modes guarded against:
    * Duplicates when pages shift under a moving feed: inserts are idempotent by
      trade_key, so overlap is harmless.
    * Gaps: a forward poll keeps paging until a page contains no new trades (or
      the offset cap, which is logged as an anomaly: a gap is possible).
    * Malformed rows: counted in ``schema_drift`` and skipped, never guessed.
    * Crashes: cursors are persisted after every page, so backfills resume.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from polybot.config import Config
from polybot.data import schemas
from polybot.data.client import PolymarketClient
from polybot.data.store import Store

log = logging.getLogger("polybot.ingest")


@dataclass
class IngestResult:
    pages: int = 0
    rows_seen: int = 0
    inserted: int = 0
    drift: int = 0
    oldest_ts: int | None = None
    newest_ts: int | None = None
    stopped_because: str = ""
    notes: list[str] = field(default_factory=list)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [r for r in payload["data"] if isinstance(r, dict)]
    raise schemas.SchemaDriftError(f"expected a list payload, got {type(payload).__name__}")


class Ingestor:
    def __init__(self, client: PolymarketClient, store: Store, cfg: Config) -> None:
        self.c = client
        self.store = store
        self.cfg = cfg

    def _parse_trades(
        self, rows: Iterable[dict[str, Any]], endpoint: str, res: IngestResult
    ) -> list[schemas.TradeRecord]:
        out = []
        for r in rows:
            try:
                out.append(schemas.parse_trade(r))
            except schemas.SchemaDriftError as exc:
                res.drift += 1
                self.store.log_drift(endpoint, str(exc))
        for t in out:
            res.oldest_ts = t.ts if res.oldest_ts is None else min(res.oldest_ts, t.ts)
            res.newest_ts = t.ts if res.newest_ts is None else max(res.newest_ts, t.ts)
        return out

    # --- global trades feed ------------------------------------------------------
    def backfill_trades(
        self, start_ts: int, end_ts: int, *, job: str = "backfill_trades", resume: bool = True
    ) -> IngestResult:
        """Page the global feed newest-first until older than ``start_ts`` or the cap."""
        ic = self.cfg.ingest
        res = IngestResult()
        cur = (self.store.get_cursor(job) if resume else None) or {}
        if cur.get("start_ts") != start_ts or cur.get("end_ts") != end_ts:
            cur = {"start_ts": start_ts, "end_ts": end_ts, "offset": 0}
        offset = int(cur["offset"])
        while True:
            if offset > ic.max_offset:
                res.stopped_because = "offset_cap"
                self.store.log_anomaly(
                    "history_cap_reached",
                    f"/trades offset cap {ic.max_offset}; oldest ts reached {res.oldest_ts}; "
                    f"requested start {start_ts}",
                )
                break
            rows = _rows(self.c.trades(limit=ic.page_limit, offset=offset))
            res.pages += 1
            res.rows_seen += len(rows)
            if not rows:
                res.stopped_because = "empty_page"
                break
            parsed = self._parse_trades(rows, "data/trades", res)
            keep = [t for t in parsed if start_ts <= t.ts < end_ts]
            res.inserted += self.store.insert_trades(keep, "api:trades")
            offset += len(rows)
            self.store.set_cursor(job, {**cur, "offset": offset})
            if parsed and max(t.ts for t in parsed) < start_ts:
                res.stopped_because = "reached_start"
                break
        return res

    def forward_poll_once(self) -> IngestResult:
        """Fetch newest trades until a page brings nothing new (or the offset cap)."""
        ic = self.cfg.ingest
        res = IngestResult()
        offset = 0
        quiet_pages = 0
        while offset <= ic.max_offset:
            rows = _rows(self.c.trades(limit=ic.page_limit, offset=offset))
            res.pages += 1
            res.rows_seen += len(rows)
            if not rows:
                res.stopped_because = "empty_page"
                return res
            new = self.store.insert_trades(
                self._parse_trades(rows, "data/trades", res), "api:trades"
            )
            res.inserted += new
            offset += len(rows)
            quiet_pages = quiet_pages + 1 if new == 0 else 0
            if quiet_pages > ic.forward_overlap_pages:
                res.stopped_because = "caught_up"
                return res
        res.stopped_because = "offset_cap"
        self.store.log_anomaly(
            "forward_gap_possible",
            f"forward poll hit offset cap {ic.max_offset} without catching up",
        )
        return res

    # --- per-wallet history -------------------------------------------------------
    def backfill_wallet(self, wallet: str, start_ts: int, end_ts: int) -> IngestResult:
        """Per-wallet TRADE history via /activity with a time window."""
        ic = self.cfg.ingest
        res = IngestResult()
        offset = 0
        while offset <= ic.max_offset:
            rows = _rows(
                self.c.activity(
                    wallet,
                    limit=ic.page_limit,
                    offset=offset,
                    start=start_ts,
                    end=end_ts,
                    cache=True,
                )
            )
            res.pages += 1
            res.rows_seen += len(rows)
            if not rows:
                res.stopped_because = "empty_page"
                return res
            trades = [r for r in rows if str(r.get("type", "TRADE")).upper() == "TRADE"]
            parsed = self._parse_trades(trades, "data/activity", res)
            keep = [t for t in parsed if start_ts <= t.ts < end_ts]
            res.inserted += self.store.insert_trades(keep, "api:activity")
            offset += len(rows)
        res.stopped_because = "offset_cap"
        self.store.log_anomaly(
            "wallet_history_cap", f"{wallet}: /activity offset cap; oldest ts {res.oldest_ts}"
        )
        return res

    # --- markets / resolutions ------------------------------------------------------
    def fetch_markets(self, ids_in: list[str], by: str = "condition_ids") -> IngestResult:
        """Look markets up by ``condition_ids`` or ``clob_token_ids`` (Gamma, UNVERIFIED)."""
        if by not in ("condition_ids", "clob_token_ids"):
            raise ValueError(f"bad lookup key {by}")
        ic = self.cfg.ingest
        res = IngestResult()
        ids = sorted(set(ids_in))
        for i in range(0, len(ids), ic.market_ids_per_request):
            chunk = ids[i : i + ic.market_ids_per_request]
            params: dict[str, Any] = {by: chunk, "limit": len(chunk)}
            rows = _rows(self.c.markets(**params))
            res.pages += 1
            res.rows_seen += len(rows)
            parsed = []
            for r in rows:
                try:
                    parsed.append(schemas.parse_gamma_market(r))
                except schemas.SchemaDriftError as exc:
                    res.drift += 1
                    self.store.log_drift("gamma/markets", str(exc))
            res.inserted += self.store.upsert_markets(parsed)
            found = (
                {m.condition_id for m, _ in parsed}
                if by == "condition_ids"
                else {t.token_id for _, toks in parsed for t in toks}
            )
            for x in sorted(set(chunk) - found):
                self.store.log_anomaly("market_not_found", f"{by}={x}")
        self.store.backfill_trade_links()
        return res

    def refresh_markets(self) -> IngestResult:
        """Look up markets referenced by trades but unknown, and re-check unresolved ones."""
        conn = self.store.conn
        unknown = [
            r[0]
            for r in conn.execute(
                """SELECT DISTINCT condition_id FROM trades WHERE condition_id IS NOT NULL
               AND condition_id NOT IN (SELECT condition_id FROM markets)"""
            )
        ]
        unresolved = [
            r[0]
            for r in conn.execute(
                "SELECT condition_id FROM markets WHERE resolution_status IN ('open', 'ambiguous')"
            )
        ]
        res = self.fetch_markets(unknown + unresolved)
        orphans = [
            r[0]
            for r in conn.execute(
                """SELECT DISTINCT token_id FROM trades WHERE condition_id IS NULL
               AND token_id NOT IN (SELECT token_id FROM tokens)"""
            )
        ]
        if orphans:  # e.g. imported on-chain history that only carries token ids
            r2 = self.fetch_markets(orphans, by="clob_token_ids")
            res.pages += r2.pages
            res.inserted += r2.inserted
            res.drift += r2.drift
        return res
