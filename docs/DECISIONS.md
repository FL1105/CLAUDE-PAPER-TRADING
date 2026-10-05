# DECISIONS

Each judgment call, why, and what would change it. New decisions are appended.

## D1. Unit of observation = (wallet, market), not trade
Multiple trades by one wallet in one market are strongly correlated (same thesis,
same outcome). Treating them as independent inflates n and the z-score.
Aggregating to one observation per (wallet, market) with a size-weighted entry
price is conservative.

## D2. Benjamini-Hochberg over Bonferroni
We test thousands of wallets. Bonferroni controls the chance of *any* false
discovery and would leave almost no power. BH controls the expected *fraction* of
false discoveries among selected wallets (q = 0.10), which is the quantity that
matters for a portfolio of copied wallets. False discoveries that slip through
are then caught by the out-of-sample gates and the placebo comparison.

## D3. Hold to resolution in v1
We do not mirror wallet exits. Exit timing adds a second latency-sensitive leg
and a second fill-quality problem, and hold-to-resolution edge is the only edge
our per-wallet test measures. Wallet sells are logged as informational.

## D4. No Kelly, no martingale, fixed fractional stake
Edge estimates are uncertain. Full Kelly on an overestimated edge overbets and
can ruin the bankroll. A fixed 0.5% stake with per-market and total exposure
caps keeps paper results interpretable.

## D5. Separate raw fetching from parsing (adapter layer)
`data/client.py` returns raw JSON; `data/schemas.py` is the only place that
knows field names. Schema drift then fails loudly in one place
(`SchemaDriftError`, counted per endpoint), and a schema fix touches one module.

## D6. Resolution only from settlement prices + closed flag
Last trade price at 0.99 is not resolution. Markets can be voided, disputed or
settled 50-50. Unclean closed states are `AMBIGUOUS` and excluded.

## D7. Config is frozen, hashed and rejects unknown keys
A typo'd threshold must not silently fall back to a default. The hash is stamped
on every paper fill; gate 12 fails if it changed during the forward test.

## D8. Deterministic retry jitter
The HTTP client's jitter RNG is seeded so test runs and retry schedules are
reproducible. Jitter still spreads load because real network timing differs.

## D9. Safety by absence, enforced by a test
There is no signing code, no key handling and no authenticated client.
`tests/test_safety.py` scans `src/` and `scripts/` for order or key related
patterns, including any HTTP verb other than GET. Adding a real-money path
requires deleting that test, which a reviewer will see.

## D10. Synthetic null world uses calibrated prices by default
The build spec draws q ~ Beta and then p0 = clip(q + noise). With that
construction prices are *not* calibrated given p0 (cheap tokens win more often
than priced), so the edge test's null hypothesis does not hold exactly even for
wallets with no information. The default `calibrated` model draws p0 first and
q | p0 with mean p0 (sd ~ sigma_noise), which makes the null hold exactly. That
isolates the question "is the test itself calibrated?". The literal spec model
is kept as `price_model: additive`, and both are reported at every checkpoint.
Real markets may be miscalibrated in the additive way (favorite-longshot bias).
That is a reason to also match placebo wallets on entry-price profile (Phase 5).

## D11. Null-world execution noise is mean-zero
The first Phase 2 calibration run found the mean null z = -0.033 (pooled KS
p ~ 1e-8). Cause: synthetic fills were placed 0 to 0.5 cents *above* the public
price, so null wallets were slight net losers. The test was right and the world
was wrong. Fills now have mean-zero noise. Real copy trading *does* pay the
spread; that cost is modeled explicitly in Phase 4 (book walk, fees), not hidden
in the null world.

## D12. Small-sample p-values by seeded Monte-Carlo from the Bernoulli null
For wallets with fewer than `stats.small_n_exact_threshold` markets the normal
approximation is unreliable, so p-values are simulated directly from
y_i ~ Bernoulli(p_i) with (1 + #exceed) / (B + 1). This is valid and slightly
conservative for a discrete statistic. Wallets this small are ineligible for
selection anyway (min 30 markets), but the report still shows them correctly.

## D13. Per-wallet detection power is low against modest skill (finding, not a fix)
In the default skilled world (3% of wallets, about 10 cents per share of edge,
about 40 in-sample markets), z is about 1.3. BH across about 540 eligible
wallets needs z of about 3.5, so it usually selects nobody. Detection appears
only with more activity or larger edge. Across all skill levels tried, the
false-discovery proportion stayed near q = 0.10. We do not loosen q to
"find" more. The system is built to say "no edge" when evidence is thin. Phase
5 will report power at the default skill and at stronger skill levels.

## D14. Experiments module
`src/polybot/experiments.py` (not in the spec layout) holds the synthetic
experiments behind `polybot simulate`. Phase 5 extends it with the full
pipeline, gates and false-promotion rate.

## D15. Leakage safety by construction, tested by mutation
The selector receives a `DataView`, never the full frames. The view copies only
trades before the cutoff and erases every resolution not known before it. The
main leakage test rewrites the future (flips all later outcomes, moves
resolution times, edits and adds post-cutoff trades) and requires a byte-identical
selection. To check that the test works, a leak was deliberately injected into
`DataView`: two leakage tests failed, and the injection was reverted.

## D16. Walk-forward window keys on resolution time
Selection for epoch k uses markets that resolved in
[start - trailing_window, start), with all of their trades. Trades in markets
that had not resolved by the cutoff are purged and counted. Scoring a wallet on
a market whose result was not yet known would be look-ahead.

## D17. Phase 3 OOS is gross, at the wallet's own price
The walk-forward scores the frozen set at the prices the wallets paid. That is
an upper bound on what a copier could get. Phase 4 replaces it with
book-walking fills, fees and latency, so the Phase 3 numbers must never be read
as achievable returns.

## D18. Imported history uses the same validator as the API
Third-party datasets are parsed by `schemas.parse_trade` (with `conditionId`
optional), so a bad mapping shows up as schema drift rather than wrong data.

## D19. Stake is a fixed fraction of the STARTING bankroll
0.5% of the initial virtual bankroll per signal: no compounding, no Kelly, no
martingale. Compounding would make paper ROI depend on the order of wins, and
Kelly needs an edge estimate we do not trust (D4).

## D20. Fill rate excludes capacity skips
Signals skipped by our own risk limits (max positions, per-market cap, total
exposure, cash) are a sizing choice, not evidence that the market could not
fill us. Gate 9's fill rate = filled / signals that reached the book. Capacity
skips are reported separately as `capacity_skipped` so a capacity-starved strategy
is still visible.

## D21. Shadow fills have no chase cap
The primary fill obeys the chase rule. Shadow fills at 1x/2x/5x L are walked
without the cap so they show what latency *would* have cost. That is the input
to the latency decay curve (gate 7).

## D22. Positions settle in time order during replay
The first replay settled everything at the end, so capacity filled immediately
and the fill rate read 0.75%. Settlement now happens as each market resolves,
freeing capacity exactly as it would in real time.

## D23. Merge book levels that share a tick-snapped price
Found by a Hypothesis property test: two asks at the same price (duplicated, or
0.501 and 0.509 both snapping to 0.51) were treated as separate levels. They are
now merged before walking.

## D24. Adverse selection at high latency (finding)
When latency is long relative to how fast prices absorb information (d/h >= 1),
the chase rule rejects about 96% of signals. The few fills that remain are the
trades where the price did *not* run away, which are disproportionately the
wallets' wrong calls (gross ROI of the filled subset is about -18% in the
skilled world). The fill-rate and latency gates must catch this. Phase 5 tests it.

## D25. Placebo is matched on activity AND entry-price profile
The spec matches placebo wallets on trade count. Phase 2 showed that a
miscalibrated market (favorite-longshot bias) makes *price level* look like skill
(D10). Each selected wallet is therefore replaced by a random eligible,
non-selected wallet from the same activity quintile and mean-entry-price tercile,
with fallback to the activity bucket only. The comparison is "skill vs. a similar
wallet", not "skill vs. any wallet".

## D26. A fast in-memory engine for placebo and stress runs, proven identical
200 placebo draws through the SQLite-backed replay would take minutes per
evaluation. `execution/fastsim.py` re-implements the same logic in memory using
the same book, fee and synthetic-book functions. `tests/execution/test_fastsim.py`
requires it to match `replay` exactly (fills, skips, settlements, ROI to 1e-9)
under normal, high-latency and stress settings.

## D27. Losing streaks are counted per market (gate 10)
The first full evaluation of a skilled world failed gate 10 with a 10-trade losing
streak against a p99 of 9. Copies in the same market resolve together with one
outcome, which inflates trade-level streaks. Per market (the unit of observation,
D1) the streak was 8. Drawdown stays trade-level (it is equity).

## D28. Simulation admissibility is separate from real decisions
The acceptance tests measure gates 2 to 12 (and gate 1's duration) on synthetic
evidence via `admissibility="simulation"`. Every real evaluation uses `strict`,
under which only `live_forward` evidence can pass gate 1. Synthetic and backtest
evidence can never promote past Common.

## D29. Run-time leakage self-check on real data
`polybot backtest` flips every future outcome and perturbs every future trade for
the first epoch, and requires an identical selection. A failure counts toward
gate 12 and the data-integrity kill switch.

## D30. Kill switches halt new risk until a human clears them
A trigger stores a halt in the database. The executor then skips every new entry
signal (`halted:<reason>`) while still logging wallet exits. `polybot resume --note`
clears it, and the note is kept in the anomaly log.

## D31. Forward fills are decided on the detection-time book; shadows are queued
On detecting a frozen wallet's BUY, the loop fetches the real book immediately
(the fill reference) and queues the signal. Shadow books at +1L/+2L/+5L are
fetched on later iterations, and only then is the fill written, stamped with the
detection time. The queue is persisted. After a crash, shadows that can no longer
be captured honestly are recorded as `missing`, never back-filled.

## D32. Stale detections are skipped, not copied
After downtime, a wallet trade seen more than `paper.max_signal_age_s` (300 s)
after it happened is skipped with reason `stale_signal`. On the very first start
the trade cursor is set to "now", so historical trades are never copied.

## D33. Live gate 4 compares like with like
The ledger's real fills cannot be compared with simulated placebo fills. For
live evidence, the strategy and 200 placebo draws are both scored by the same
engine (tape reference + synthetic book) on the same forward-period trades. The
report shows that engine ROI next to the ledger ROI.

## D34. Tape-priced latency curves are coarse
On real-data backtests the shadow price is the last *trade* in the token within
the delay. When trading is sparse, 1x/2x/5x L (seconds) often see the same
trade, so the curve is flat. Only the forward run's real shadow books measure
latency decay properly. The synthetic demo shows this flat curve.

## D35. Acceptance results are reported against the world, not tuned to it
Default-strength skill is almost never promoted (low power). Strong skill is
blocked by the 2-cent slippage cap and the 2x stress gate under the 1-cent
synthetic half-spread, and passes at 0.5 cents. We do not loosen gates to reach
the spec's 80% power. The binding constraint is copy cost, which the forward
run's real books will measure.
