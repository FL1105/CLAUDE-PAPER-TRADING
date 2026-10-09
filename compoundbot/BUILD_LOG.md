# BUILD LOG
Newest last. Each entry: built / measured / unverified.

## Preflight + Step 0 + Step 1
Built: project skeleton, pinned-range deps, CLAUDE.md, config models (extra=forbid), risk.yaml hash check.
Measured: network preflight (all market-data hosts blocked; see docs/ASSUMPTIONS.md). Tests: config loads, bad values rejected, hash change detected.
Unverified: broker/API facts (no network). Inception dates in universe.yaml are ESTIMATED.

## Step 2 - Data layer
Built: NYSE calendar (rule based), seeded SYNTHETIC generator (vol clustering, planted-edge / null worlds),
parquet cache (atomic, incremental), providers (CSV importer, synthetic, UNVERIFIED Alpaca REST), quality checks.
Measured: tests pass (determinism, planted edge raises trend autocorrelation, quality flags gaps/jumps).
Unverified: Alpaca bars field names; real-data quality report not produced (no network).

## Steps 3-5 - Backtest engine, statistics, baselines
Built: engine (next_open / next_close, no-trade band, min order, fixed fees, drawdown ladder with re-risk/re-arm), cost model,
metrics, stats (stationary bootstrap, PSR, DSR, MinTRL, paired bootstrap, placebo p), baselines (SPY, 60/40, EW, cash, vol-targeted EW and 60/40).
Measured (tests): lookahead perturbation test, hand-computed equity (both exec modes), exact cost difference, x2 cost doubles cost,
determinism, metric formulas, bootstrap SE ~ 1/sqrt(T).
Found while testing: the Sharpe-proof times in the master prompt draft were wrong (SR^2/2 term is divided by 252 when annualised);
corrected in prompt v3 (SR=1: ~2.7y one-sided, ~3.8y two-sided; SR=0.5: 11-15y). Also fixed a drawdown bug (starting equity 1.0 is a peak).
Unverified: real-data baselines (no network).

## Steps 6-8 - Strategies, research harness, portfolio
Built: strategies A-D (+ cash as the off state), registry with pre-registered grids, walk-forward harness (expanding window, holdout split,
experiment log, placebo via circular time-shift, automated E1-E6 verdicts), report writers, inverse-vol sleeve combination + Ledoit-Wolf vol targeting.
Measured (SYNTHETIC only): see docs/results/ for harness acceptance on planted-edge and null worlds.
Unverified: no real-data research verdicts exist. GATE 1 is waiting on real data.

## Steps 9-11 - Risk, brokers, daily runner
Built: limits (clip weights, order checks), kill switch (file flag, persistent), ladder, Paper-local broker (same cost model), Alpaca paper adapter
(UNVERIFIED, pinned to the paper host), live stub (raises), order planner, reconciliation, idempotent daily runner with state + run records.
Measured (tests): hypothesis fuzz on limits and ladder, kill-switch drill, mocked-HTTP broker flow, idempotent re-run, stale-data block,
30-day replay equals full-history targets, missed-day recovery, safety scan.
