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
| 5 | gates, stress, placebo, bootstrap, ladder, kill switches, acceptance tests | **done**, see `docs/CHECKPOINT_PHASE5.md` |
| 6 | forward paper loop, settle, report, dashboard, runbook | **done**, see `docs/CHECKPOINT_PHASE6.md` |
| 7 | optional strategy B (`btc_lag`) | not started (optional) |

**Important:** Phase 1 was built without network access to Polymarket, so the
Update 2026-10-06: `polybot probe` has run against the real API (no drift); first real-data results
are in `docs/RESULTS_2026-10-06.md` (short version: no copyable wallet edge found).
API schemas were originally *assumed*. Run `polybot probe` first. See
`docs/ASSUMPTIONS.md` and `docs/RUNBOOK.md`.

## Quick start

**Windows:** `powershell -ExecutionPolicy Bypass -File .\setup.ps1` (see docs/RUNBOOK.md, section 1b).
**Mac / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
polybot doctor                    # checks this machine; says what to fix
polybot probe                     # verify the real API first
polybot backfill --days 90        # and/or: polybot history import ...
polybot backtest --days 90        # can reach tier Common at most
polybot paper                     # the forward run: leave it running 30+ days
polybot report                    # then open dashboard.html
```
Full instructions: `docs/RUNBOOK.md`. Results and what is verified: `docs/FINAL_SUMMARY.md`.

## Research tools added after the first build

```bash
polybot history import|markets    # real history from the Hugging Face dataset (docs/HISTORY_IMPORT.md)
scripts/hf_pipeline.sh NAME START END SAMPLE   # one windowed walk-forward run, own DB in out/NAME/
polybot fleet init|run|report     # several candidate configs side by side + comparison page
polybot maker record|sim          # record real books/tape, replay a conservative passive maker
scripts/markout_study.py          # taker markout (what liquidity providers earned)
scripts/btc_updown_study.py       # calibration of BTC up/down markets near expiry
scripts/overnight.sh              # unattended research queue (needs a machine that stays up)
```

## Layout

```
config/default.yaml     every threshold and parameter
docs/                   FINAL_SUMMARY, ASSUMPTIONS, DECISIONS, GATES, RUNBOOK, HISTORY_IMPORT, CHECKPOINT_*, results/
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
  execution/fastsim.py  in-memory copy engine for placebo/stress (proven identical)
  gates/metrics.py      ROI, drawdown, streaks, concentration, Evidence
  gates/gates.py        the 12 paper gates (pure functions -> GateResult)
  gates/ladder.py       rarity tiers, kill switches, micro-live (S3) evaluator
  backtest.py           walk-forward -> fills -> stress -> placebo -> gates
  forward.py            live paper loop: frozen sets, real books, shadows, settle, kill switches
  report/build.py       live evidence -> out/report.json, trades.csv, equity.csv
dashboard.html          single-file offline dashboard (open locally; phone friendly)
scripts/demo_report.py  synthetic demo report to preview the dashboard
tests/                  unit tests; fixtures/assumed (hand-written), fixtures/live (from probe)
```
