# GATE (pre-registered; thresholds are not edited after seeing results)

Edge criteria are evaluated by `src/bot/backtest/walkforward.py::evaluate` on the walk-forward out-of-sample
period of the development sample (the final 2 years are frozen as a holdout). Implementation criteria are
evaluated by `src/bot/monitor/gate.py` from paper runs. `python -m bot.cli gate` prints the table and writes
`reports/gate_status.json`. SYNTHETIC research can never pass the overall gate.

## EDGE (a strategy must pass ALL to be KEPT)
| id | criterion | threshold |
|---|---|---|
| E1 | Net Sharpe 95% stationary-bootstrap CI lower bound; Deflated Sharpe (all logged trials, Sharpe variance across trials) | lower bound > 0 and DSR >= 0.90 |
| E2 | Paired bootstrap vs the best-Sharpe baseline (including vol-targeted equal-weight and 60/40), or same-or-better CAGR with max DD at most 80% of the baseline's; plus shuffled-signal placebo | p < 0.10 (or the drawdown alternative) and placebo p < 0.10 |
| E3 | Net Sharpe at 2x costs; cost-cover (gross / cost) if turnover exceeds 12 full turns a year | Sharpe(x2) > 0; cover >= 2 |
| E4 | OOS max drawdown with the ladder on; worst calendar year | <= halt level + 5 points (20%); >= -10% |
| E5 | Neighbouring grid values keep Sharpe | >= 70% of the chosen value |
| E6 | No single year above 40% of positive-year return; covered stress regimes flat or better (>= -2%) | <= 40%; >= min(3, covered) |
| E7 | Holdout (evaluated once): net Sharpe positive and its CI lower bound not above the best walk-forward Sharpe | pass/fail in reports/holdout.md |

## IMPLEMENTATION (paper)
| id | criterion | threshold |
|---|---|---|
| I1 | 60 trading days, zero error/halt runs, zero unreconciled positions | all |
| I2 | Daily tracking error vs backtest replay; cost paid vs modelled | < 15 bps; within 25% |
| I3 | Kill-switch, drawdown-halt, crash-recovery drills (`python -m bot.cli drills`) | all True |
| I4 | Realised vol vs target | 75% to 125% |

Caveats written down in advance:
- 18-20 years of ETF history hold only a few independent regimes; passing E1-E6 is evidence, not proof.
- A 2-year holdout has a Sharpe standard error of about 0.7. E7 is a sanity check.
- Paper fills are idealised, so I2 tests plumbing and sizing, not real slippage.
- Costs in `config/costs.yaml` are ESTIMATED. Replace with measured broker fees and slippage before trusting any net number.

## Status (updated at the end of the build)
Real-data research has not been run (no market-data access during the build). SYNTHETIC checks only: see BUILD_LOG.md.
