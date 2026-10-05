# Phase 2 checkpoint: does the test say "no edge" when there is none?

Reproduce: `polybot simulate --world null --seeds 300` (and `--price-model additive`,
`--world skilled --seeds 50`). Raw output: `out/simulate_*.json`. All runs seeded
from `config.seed` and byte-identical on re-run.

Setup per seed: 3,000 markets, 2,000 wallets (heavy-tailed activity, 2% market
makers), ~1,080 wallets eligible (>= 30 resolved markets) per seed.

## 1. Null world, calibrated prices (the acceptance test)

| check | result | expected if the test is valid |
|---|---|---|
| per-seed KS uniformity fails (p <= 0.01) | **4 / 300 = 1.3%** (Wilson 95%: 0.5% to 3.4%) | 1% |
| KS of the 300 per-seed KS p-values | p = 0.15 | uniform |
| rejection rate at alpha = 0.05 | **5.08%** of 323,784 wallet tests | 5% |
| rejection rate at alpha = 0.01 | **0.94%** | 1% |
| BH (q = 0.10): seeds with any discovery | **14 / 300 = 4.7%** (2.8% to 7.7%) | at most 10% |
| small-n wallets, exact Monte-Carlo p < 0.05 | 4.6% (normal approximation: 4.8%) | at most 5% |
| pooled KS over all 323,784 p-values | stat 0.0042, p = 2e-5 | see note |

**Verdict: PASS.** The per-seed criterion from the spec holds, and rejection
rates match nominal to two decimals.

Note on the pooled KS: with 324k p-values, KS detects a 0.4% maximum CDF
deviation. That is the discreteness of Bernoulli sums plus shared market outcomes
within each seed, which makes pooled p-values non-independent. The deviation is
practically irrelevant (rejection rates are on target), but it is reported rather
than hidden. A 40-seed pooled run gave p = 0.47.

## 2. Selection bias demo (null world, 50 seeds, in-sample first 90 days, OOS last 90)

| selector | in-sample ROI | OOS ROI | OOS pooled Z | OOS wallets with positive edge | wallets selected |
|---|---|---|---|---|---|
| naive top-20 by PnL | **+90.4%** | **+0.3%** | -0.05 | 50.9% | 20 |
| BH edge test | n/a | n/a | n/a | n/a | **0.04 per seed** |

The naive list looks spectacular in-sample and is worth nothing out-of-sample.
This is the "profitable in the last 90 days" trap, measured. BH almost never
selects anyone from a world with no skill.

## 3. Literal-spec price model (`additive`: prices miscalibrated given p0)

| check | result |
|---|---|
| per-seed KS fails | 1.0% |
| pooled KS | stat 0.0098, p ~ 0: clearly non-uniform |
| rejection at 0.05 / 0.01 | 5.6% / 1.1%: slightly anti-conservative |
| BH seeds with any discovery | 10.3% (at the q bound) |
| naive top-20: in-sample, OOS ROI | +195%, **+10.3%** |

Here the null hypothesis ("price is calibrated") is false by construction:
cheap tokens win more often than priced. The naive list then keeps a real but
non-skill edge out-of-sample (buying underpriced longshots). If real Polymarket
has favorite-longshot bias, *price-level* strategies will look like wallet skill.
Phase 5's placebo must therefore match wallets on entry-price profile, not just
trade count (DECISIONS D10).

## 4. Skilled world (3% skilled wallets, default strength, 50 seeds)

| selector | skilled wallets picked | false discovery proportion | OOS ROI | OOS pooled Z |
|---|---|---|---|---|
| naive top-20 by PnL | 3.7 of 20 | **81%** | +8.6% | 1.55 |
| BH | 0.48 (of 27 eligible) | **4.8%** | +31.3% | 1.81 |

BH is precise (95% of picks are truly skilled) but has **low power** at this skill
strength. Detection rises sharply with activity or edge (DECISIONS D13):

| sigma_noise, scan multiplier | BH picks / seed | FDP | OOS ROI |
|---|---|---|---|
| 0.08, 3 (default) | 1.2 | 13% | +11% |
| 0.08, 8 | 6.4 | 7% | +26% |
| 0.12, 8 | 19.6 | 9% | +34% |
| 0.15, 8 | 31.3 | 10% | +40% |

(10 seeds each; FDP stays near q = 0.10 at every strength.)

## What this means for real data
- The machinery says "no edge" when there is none, and the false-positive
  control is verified.
- Expect few or no BH-selected wallets on real data unless some wallets have large,
  sustained edge over many markets. That is a legitimate outcome, not a bug.
- Market miscalibration can masquerade as skill; Phase 5 adds the controls.
