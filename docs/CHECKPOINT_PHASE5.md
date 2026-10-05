# Phase 5 checkpoint: gates, false-promotion rate, power

Reproduce: `python scripts/phase5_acceptance.py` (about 30 min on 4 cores). Summaries
(per-seed runs removed) are in `docs/results/acceptance_*.json`.

Every run is the **full pipeline**: synthetic world -> 8 walk-forward epochs (14 d,
60-day trailing window) -> BH selection -> realistic fills (latency drift,
synthetic book, fees, risk limits, chase rule) -> 2x stress -> 200-draw matched
placebo -> all 12 paper gates. Gate 1's mode check uses the labeled `simulation`
admissibility (real evaluations are `strict`, where synthetic evidence can never
pass; DECISIONS D28). d/h = latency / information half-life.

## Results

| world | seeds | promoted | rate (Wilson 95%) | seeds with trades | mean net ROI |
|---|---|---|---|---|---|
| **null, calibrated prices** (acceptance 1) | 300 | **0** | **0.0% (0.0% to 1.26%)** | 98 | -0.9% |
| null, literal-spec prices (miscalibrated) | 300 | 0 | 0.0% (0.0% to 1.26%) | 120 | -6.0% |
| skilled, default strength, d/h = 0.1 | 100 | 0 | 0.0% (0.0% to 3.7%) | 76 | +3.6% |
| skilled, strong, d/h = 0.1, 1 c half-spread | 100 | 0 | 0.0% (0.0% to 3.7%) | 100 | +19.5% |
| **skilled, strong, d/h = 0.1, 0.5 c half-spread** | 100 | **60** | **60% (50% to 69%)** | 100 | +24.6% |
| **skilled, strong, d/h = 5 (edge priced in)** (acceptance 4) | 100 | **0** | 0.0% (0.0% to 3.7%) | 100 | -11.6% |

Gate pass rates (%), gates 1 to 12:

| world | 1 | 2 | 3 | 4* | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| null calibrated | 100 | 0 | 1 | 0 | 2 | 8 | 15 | 0 | 32 | 33 | 13 | 100 |
| null literal-spec | 100 | 0 | 1 | 0 | 3 | 10 | 16 | 0 | 39 | 40 | 12 | 100 |
| skilled default | 100 | 4 | 11 | 0 | 16 | 23 | 44 | 0 | 32 | 76 | 35 | 100 |
| strong, 1 c spread | 100 | 100 | 97 | 0 | 100 | 11 | 100 | 51 | **0** | 100 | 88 | 100 |
| strong, 0.5 c spread | 100 | 100 | 99 | 60 | 100 | 87 | 100 | 72 | 100 | 99 | 96 | 100 |
| strong, high latency | 100 | 5 | 1 | 0 | 100 | 14 | 20 | 0 | **0** | 61 | 4 | 100 |

\* Gate 4 (placebo) is computed only when every other gate passes (lazy mode; the
promotion decision is identical). A 0 means "not reached", not "failed the placebo".

## Against the spec's acceptance tests

1. **Null world false-promotion rate <= 1%: PASS** on the point estimate: 0 of 300,
   Wilson upper bound 1.26%. Proving the true rate is below 1% at 95% confidence
   would need about 385 or more seeds with zero promotions.
2. **Null p-value uniformity (KS): PASS** (Phase 2: per-seed KS fails 1.3% vs 1%
   expected; rejection rate 5.08% at alpha = 0.05).
3. **Skilled world, low latency, gates pass in >= 80% of seeds: NOT MET.**
   - Default strength: 0%. Per-wallet skill is too weak to survive BH over
     ~540 wallets in a 60-day window (Phase 2, D13).
   - Strong skill with the default 1 c synthetic half-spread: 0%. Every seed
     fails **gate 9** (median slippage just above the 2 c cap) and most fail
     **gate 6** (2x stress).
   - Strong skill with a 0.5 c half-spread: **60%** (50 to 69%). The remaining
     failures are mostly gate 4 (placebo) and gate 8 (concentration).

   **Gates were not loosened to reach 80%** (spec: "never tune thresholds to make
   a result pass"). The binding constraint is *copy cost*, which the synthetic
   book only guesses. Real spreads and depth from the forward run's books
   decide this on real data.
4. **Skilled world, high latency: PASS.** 0 of 100. Fillability fails in every
   seed (the chase rule rejects most signals) and the latency/stress gates mostly
   fail. Edge that exists only at zero latency does not promote.
5. **Naive top-by-PnL in the null world: PASS** (Phase 2: +90% in-sample, +0.3%
   out-of-sample).
6. **Leakage canary: PASS** (Phase 3: the leaky selector shows +138% OOS; the real
   selector cannot reproduce it; the future-mutation test is verified to fail
   on an injected leak).

## What this means
The system almost never promotes luck. It promotes real skill only when the
skill is strong AND cheap to copy. On real data, expect "no edge" unless some
wallets have large, persistent edge in liquid markets. That is the honest
answer the system was built to give.
