# Research harness acceptance (SYNTHETIC)

SYNTHETIC data only. Purpose: check that the walk-forward harness and its gates KEEP a strategy that has a planted
edge and REJECT one in a null world. This says nothing about real markets.

Setup: `make_prices(seed, planted_edge)` 2005-2022, last 2 years held out (not used), expanding walk-forward
(4y min train, 1y test), pre-registered grids from config/strategy.yaml, costs x1 and x2, placebo = 40 circular
time-shifts, 200 bootstrap draws per test, DSR counting every logged trial. Strategies: trend, momentum_rotation.
Script: scripts/harness_acceptance.py (n_placebo/n_boot raised for the real run).

| world | seeds | strategy runs | KEEP | REJECT |
|---|---|---|---|---|
| planted edge (edge_strength=4, far above reality) | 1-6 | 12 | 9 | 3 |
| null (no predictability, +3% drift) | 1-6 | 12 | 0 | 12 |

False-KEEP rate in the null world: 0/12 (upper 95% bound about 22% with n=12; rerun with more seeds via
`python scripts/harness_acceptance.py null 7 trend` etc.). Power in the planted world: 75%.

Not covered: mean_reversion and crypto_trend were not part of this acceptance batch (runtime). They share the
same gate code; their lookahead tests pass (tests/unit/test_strategies.py).
