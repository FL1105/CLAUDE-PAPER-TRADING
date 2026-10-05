# Phase 3 checkpoint: data layer, selection, leakage-safe walk-forward

## Built
- **Store** (`data/store.py`): SQLite with the spec's tables plus `ingest_state`
  (resumable cursors), `anomalies` and `schema_drift`. Trade inserts are idempotent;
  `paper_*` tables are append-only, enforced by triggers.
- **API ingestion** (`data/ingest.py`): resumable global backfill, forward poll
  that pages until caught up, per-wallet `/activity` history, and market lookups
  by condition id or token id. Hitting a pagination cap is logged with the oldest
  timestamp actually reached.
- **Bulk history import** (`data/history.py`, `polybot history inspect|import`):
  any CSV, Parquet or remote file DuckDB can read; date window pushed down; same
  validator as the API.
- **Resolution handling**: resolved / void / ambiguous / open (Phase 1 adapter),
  stored per market; only `resolved` markets enter edge statistics.
- **Eligibility filters** (`selection/filters.py`): market makers, entry price
  band, hyperactivity, early exits, min markets, min span, single-market
  concentration; wash-trading flagged. Every step logs how many it removed.
- **Walk-forward** (`selection/walkforward.py`): 14-day epochs, 60-day trailing
  window of resolved markets, BH selection frozen at epoch start, OOS scoring,
  naive top-PnL contrast, leakage canary.

## Leakage tests (the build fails if any of these fail)
| test | what it does | result |
|---|---|---|
| future-mutation invariance | flip every post-cutoff outcome, move resolution times, edit and add post-cutoff trades; the selection must be identical | pass |
| purge check | markets resolving after the cutoff are excluded; the window equals exactly the markets resolved in [start-60d, start) | pass |
| canary | a deliberately leaky selector (ranks by PnL in the evaluated epoch) | OOS ROI **+138%**, 97% of picks positive (obviously inflated) |
| real selector, same null world | | nobody selected; naive top-PnL OOS ROI about 0 |
| **test of the test** | injected a leak into `DataView` | 2 leakage tests failed as intended; injection reverted |

## Walk-forward on synthetic worlds (120 days, 8 epochs, gross at wallet prices)
| world | BH-selected OOS ROI | mean OOS z of selected | naive top-20 OOS ROI | leaky canary OOS ROI |
|---|---|---|---|---|
| null (seed 7) | nobody selected | n/a | +6.5% (noise) | +138% |
| skilled, strong (seed 7) | +42% | 1.22, 77% positive | +22% | n/a |

These are *gross* numbers at the wallet's own price. They are not achievable
returns; Phase 4 adds fills, fees and latency.

## Bugs found and fixed in this phase
1. **Hex IDs corrupted by CSV type sniffing**: DuckDB read `0xc` as the integer 12.
   CSVs are now read as text and ID columns are cast to strings, with a regression test.
2. Market makers never appeared in the filter counts because they only trade both
   sides. The market-maker filter now runs first and counts them.

## Open items for real data
- Real pagination caps, `condition_ids` / `clob_token_ids` lookup parameters and
  `/activity` time windows are unverified: run `polybot probe`.
- Bulk dataset layouts are unverified: `polybot history inspect` first.
- At default synthetic skill, per-wallet power over a 60-day window is low
  (DECISIONS D13): expect few selections on real data.

## Test suite
129 tests passing (plus 1 skipped until live fixtures exist); coverage 93% overall,
95 to 100% on stats/, selection/ and data/.
