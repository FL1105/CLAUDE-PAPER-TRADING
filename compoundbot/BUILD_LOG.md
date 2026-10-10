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

## Steps 12-14 - Monitoring, weekly review, dashboard
Built: drift monitor (rolling Sharpe/vol band, tracking error, auto cash mode), weekly review writer, hypothesis queue with strict
validation (can name only a registered strategy + its own grid; never risk/gate/live), challenger-beats-champion test (CI lower bound > 0
at x1 and x2 costs), gate bookkeeping (I1-I4 + edge checklist), drills (kill switch, drawdown halt, crash recovery), FastAPI dashboard
(read-only plus token-protected kill/pause; no endpoint can change limits or enable live) with a single static page.
Step 12 (ML meta-model) skipped by design.

## Steps 15-16 - holdout, paper soak, go-live prep
Built: `holdout` command (single use; refuses a second run), `paper`/`nightly`/`gate` commands, RUNBOOK, GATE, SOURCES.
Not run: holdout and the 60-day soak (no real data, no time). No real-money adapter exists (checklist only, by design).

## Defects found by the tests/drills while building (all fixed)
1. Drawdown metric ignored the starting equity as a peak.
2. Master-prompt Sharpe-proof times were wrong (SR^2/2 term); corrected to SE = 1/sqrt(T).
3. Backtest cost bookkeeping let fully invested weights drift above 1.0 after fees.
4. Forced flatten after a HALT was blocked by the order-size cap, and the kill switch engaged before the sells were sent.
5. The first runner filled orders at the same close that produced the signal (lookahead). Now queued and filled at the next open,
   trade days match the backtest, stale queued orders expire.
6. Gate checklist counted REJECTED strategies' failures against the portfolio gate.

## Measured (SYNTHETIC only)
- Harness acceptance: planted edge KEEP 9/12, null 0/12 false KEEP (docs/results/harness_acceptance.md).
- Paper runner vs engine replay, 60-120 sessions: daily tracking error ~5 bps (gate threshold 15 bps).
- Full pipeline on SYNTHETIC data (data -> research -> select -> backtest -> 25-day paper replay -> drills -> gate): runs end to end.
  Gate correctly reports not passed (60-day soak and replay checks pending, research is SYNTHETIC).

## WHAT COULD I BE WRONG ABOUT?
- Every performance number here is SYNTHETIC. No claim about real-market profitability is supported.
- Alpaca adapters were written without network access: endpoints and field names are UNVERIFIED.
- Cost inputs are estimates; real fees, spreads and crypto minimums may be worse.
- The ladder re-risk rules, sleeve weighting and the vol-target cap are design choices not yet stress-tested on real regimes.
- The synthetic planted-edge world is far stronger than reality; passing it proves the harness can detect an edge, not that one exists.

## Learning loop (costs, research budget, shadow, promotion)
Built: cost learning from fills (trimmed mean + bootstrap CI, shrinkage, bounded, increases automatic / decreases signed, history log,
break-even cost multiplier, order-size cost table), `costs_learned.yaml` merged by `load_costs` only when signed, research budget
(quarterly cap on new variants, rising Deflated-Sharpe bar, refuse-before-evaluating), proposal dedupe, learning cycle, shadow
evaluation on post-registration sessions, operator-only promotion with backup and evidence log. CLI: budget, propose, learn, shadow,
promote, costs. Docs: docs/LEARNING.md, RUNBOOK 3b. Master prompt gained a learning section.
Measured (tests): simulator fills excluded; 3x-worse costs -> bounded increase; decreases refused with few fills; over-budget refused
with nothing logged; promotion refused without evidence/name and reversible; identical challenger never promotable.
Unverified: no real broker fills exist, so nothing has actually been learned yet.

## Adversarial review round 1 (high effort over src/): 10 findings + 5 minor, all fixed with regression tests
1. Daily-loss halt could never fire live (baseline equity was read from the same snapshot) and would have liquidated the book.
   Now: baseline = previous run's closing equity; response = no buys (sells that reduce risk still go), like the backtest.
2. `paper` used the newest cached bar as "today", so stale data was never detected and the heartbeat alarm could not trip.
   Now: asof = last completed NYSE session by wall clock (`last_completed_session`); `--refresh SOURCE` re-fetches first.
3. `costs --apply` without `--by` applied decreases under an automatic signature. Now refused; unattended increases keep a human signature.
4. Drift monitor existed but nothing called it. Now wired into `nightly`: writes state/drift.json, `CASH_MODE` on persistent shortfall, alerts.
5. Early exits (killed / stale / no selection) stamped the day as run and left settled orders pending (double-reported fills, skipped ladder
   update on rerun). Now: pending ids cleared at settle, early exits do not stamp the day, run.json merges fills from earlier attempts.
6. Crypto weekend rows turned the whole panel into a 365-day calendar. Now every series is aligned to NYSE sessions.
7. Fixed fees were not scaled in x2 cost stress runs. Fixed.
8. ADV cap compared share volume with dollars. Now uses volume x close (synthetic volume is shares too).
9. Alpaca cancelled/expired-after-partial-fill orders lost their filled quantity (now `partial`); crypto bars use the crypto endpoint;
   one failing symbol no longer aborts the whole data fetch.
10. Strategy weights were built twice (x1 and x2 costs); now built once. Shadow evaluation builds each book once.
Minor: smoothed one-sided paired-bootstrap p-value, proposal values must be finite/positive/integer-typed, zero-length walk-forward and
holdout windows rejected, proposals that crash after spending budget are closed, naive datetimes read in the schedule timezone.
Added: Yahoo Finance data provider (UNVERIFIED, mocked tests) so real data needs no manual CSV collection.

## Adversarial review round 2 (broker adapter, order timing, data): 11 findings, all fixed with regression tests
1. Alpaca limit prices had sub-penny precision (would be rejected): now rounded to the tick in the safe direction.
2. Alpaca reports crypto positions as BTCUSD: now mapped back to BTC/USD (else the bot re-buys every rebalance).
3. Crypto daily bars were labelled by their UTC start day, leaking the close (~19:00 ET) into the same day's 16:00 ET
   decision: now labelled by the date the close became known (+1 day). Limitation documented in ASSUMPTIONS.
4. A drawdown HALT now re-issues flatten sells each run until flat (manual kills still stop everything).
5. Queued orders only settle against THIS session's own open print; a stale feed can no longer fill them at an old price.
   Data must now have the asof bar (max lag 0).
6. A broker error on one order no longer loses the others; orders live at the broker are recorded even if a later step raises.
7. `data` clears the previous cache first (no synthetic bars under a MEASURED tag), lists missing symbols, requires SPY/IEF/BIL.
8. Aligned series never extend past a symbol's last real bar, so a dead feed cannot look fresh.
9. Notes explaining a blocked run were being overwritten; fixed.
10. Alpaca quantities are floored, never rounded up; dust positions (< $1) no longer trigger exit orders forever.
11. A failing alert webhook is now reported as failed.
Also found while testing: stale-order expiry used the last completed run date; now uses the date the orders were submitted.
Added for the first real-data session: `probe` (read-only Alpaca check, optional 1-share paper round trip, redacted output),
`summary` (one file to paste back), scripts/tonight.sh and tonight.ps1.
