# polybot

A **paper-trading research system** for Polymarket. It answers one question
honestly: *is there a real, copyable edge in following statistically selected
wallets?* "No edge" is a valid, successful answer.

- **No real money.** v1 has no order signing, no keys, no wallet connection, and
  only public read-only GET endpoints. `LIVE_TRADING_ENABLED = False`;
  `LiveExecutor` raises. A test enforces this.
- **Paranoid statistics.** Per-wallet edge z-tests on (wallet, market) units,
  Benjamini-Hochberg FDR, leakage-safe walk-forward, placebo wallets, and
  clustered bootstrap CIs. All of it is validated on synthetic null and skilled
  worlds before real data is trusted.
- **Pessimistic fills.** Real order-book walks, fees, chase limits and
  shadow-latency fills.
- **Promotion gates.** 12 pre-registered gates (`docs/GATES.md`) decide whether
  a strategy has earned the right to *consider* real money.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | scaffold, config, API client, adapter layer, probe, docs | **done** (API schemas unverified, see below) |
| 2 | stats core, synthetic worlds, null calibration | **done**, see `docs/CHECKPOINT_PHASE2.md` |
| 3 | store, backfill, history import, resolution, selection filters, walk-forward + leakage tests | **done**, see `docs/CHECKPOINT_PHASE3.md` |
| 4 | execution: book walk, fees, chase, paper executor, ledger, replay | **done**, see `docs/CHECKPOINT_PHASE4.md` |
| 5 | gates, stress, placebo, bootstrap, ladder, kill switches | next |
| 6 | forward paper loop, settle, report, dashboard, runbook | |
| 7 | optional strategy B (`btc_lag`) | |

**Important:** Phase 1 was built without network access to Polymarket, so the
API schemas are *assumed*. Run `polybot probe` first. See
`docs/ASSUMPTIONS.md` and `docs/RUNBOOK.md`.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
polybot probe
polybot config-hash
```

## Layout

```
config/default.yaml     every threshold and parameter
docs/                   ASSUMPTIONS, DECISIONS, GATES, RUNBOOK, HISTORY_IMPORT, CHECKPOINT_*
scripts/probe_apis.py   API probe (same as `polybot probe`)
src/polybot/
  config.py             frozen pydantic config + hash
  data/client.py        rate-limited, retrying, caching GET-only client
  data/schemas.py       adapter layer: raw JSON -> validated records
  data/probe.py         endpoint probe + ASSUMPTIONS writer
  execution/fees.py     parabolic fee model (the only place fee math lives)
  execution/book.py     order-book walk (tick, rounding, chase cap, partial/reject)
  execution/latency.py  drift model, tape reference, synthetic book
  execution/paper.py    PaperExecutor: risk limits, chase rule, fills, settlement
  execution/replay.py   OOS signals -> realistic fills (net ROI, latency decay)
  execution/live.py     stub only (raises)
  strategies/           Strategy protocol + Strategy A (wallet_copy)
  data/store.py         SQLite schema, idempotent upserts, append-only paper ledger
  data/ingest.py        API backfill / forward poll / market resolution refresh
  data/history.py       bulk history import (CSV/Parquet/hf:// via DuckDB)
  data/synthetic.py     seeded null / skilled worlds
  selection/filters.py  eligibility + anti-fake-alpha filters (counted)
  selection/walkforward.py  DataView (leakage-proof), epochs, frozen sets, OOS
  stats/                edge z-test, BH-FDR, cluster bootstrap + power, calibration
  experiments.py        synthetic experiments behind `polybot simulate`
  gates/ report/        (later phases)
tests/                  unit tests; fixtures/assumed (hand-written), fixtures/live (from probe)
```
