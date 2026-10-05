# GATES

Thresholds live in `config/default.yaml`; this file mirrors them with the
reasoning. **They were fixed before any evaluation.** Change them only by
editing the config by hand, bumping `config_version`, and adding a changelog
entry below. Never change them to make a result pass.

Status: thresholds are defined (Phase 1); gate functions are built in Phase 5.

## Stage ladder

| Tier | Stage | Meaning |
|---|---|---|
| Common | S0 walk-forward backtest valid | Historical OOS shows positive net edge across epochs. A backtest alone can never promote past this tier. |
| Uncommon | S1 forward paper running | Live-forward paper trading with real book snapshots; minimum duration and trade count reached. |
| Rare | S2 paper gates passed | All 12 paper gates pass. Eligible to *consider* micro-live, pending the manual checklist. |
| Epic | S3 micro-live passed | Future task. Evaluator only in v1. |
| Legendary | S4 scaled | Future task. |

## Paper gates (S1 to S2); all must pass

| # | Gate | Threshold (config key) | Why |
|---|---|---|---|
| 1 | Forward, not simulated | evidence mode `live_forward`, >= 30 days (`gates.min_forward_days`) | Backtests leak in ways we haven't thought of. Only forward data with real books counts. |
| 2 | Sample size | >= 300 settled trades (`min_settled_trades`), >= 120 markets (`min_distinct_markets`), >= 3 epochs (`min_epochs`) | Below this, the CI on ROI is too wide to separate a few % edge from zero. |
| 3 | Net edge credible | lower bound of a one-sided 95% market-clustered bootstrap CI on ROI > 0, 5000 resamples (`bootstrap_ci_level`, `stats.bootstrap_resamples`) | Mean ROI > 0 is easy by luck; the CI bound is not. Clustering by market handles correlated positions. |
| 4 | Selection adds value | ROI > 95th pct of placebo (random matched wallets) (`placebo.required_percentile`) | If random wallets do as well, selection is not the source of edge. |
| 5 | Persistence | pooled OOS Z > 1.64 (`persistence_min_pooled_z`) and >= 55% of selected wallets positive OOS (`persistence_min_positive_frac`) | Skill should persist out of sample; luck regresses to ~0 and ~50%. |
| 6 | Cost robustness | ROI > 0 at 2x fees, 2x slippage, 2x-latency fills (`fees.stress_multiplier`) | Our fee and slippage models are estimates; edge must survive them being wrong by 2x. |
| 7 | Latency robustness | ROI vs latency curve does not cross zero before 2xL (`latency_must_survive_multiplier`) | Edge that only exists at zero latency is not ours to capture. |
| 8 | Not a lucky streak | ROI > 0 without top 3 trades (`remove_top_k_trades`); no market > 10% (`max_single_market_pnl_share`), no wallet > 35% (`max_single_wallet_pnl_share`) of PnL | One lucky outcome must not carry the result. |
| 9 | Fillability | fill rate >= 85% (`min_fill_rate`), median slippage <= 0.02 (`execution.max_median_slippage`) | A strategy that only works when you can't fill is not a strategy. |
| 10 | Drawdown | max settled DD <= 20% of bankroll (`max_drawdown_frac`); longest losing streak within the 99th pct implied by the win rate (`losing_streak_percentile`) | Large DD is unacceptable risk; an implausible losing streak flags a broken model. |
| 11 | Stability | ROI > 0 in >= 60% of epochs (`min_positive_epoch_frac`); no epoch worse than -5% of bankroll (`max_epoch_loss_frac`) | Edge concentrated in one epoch is regime luck. |
| 12 | Data integrity | 0 leakage-test failures, 0 schema-drift errors in last 7 days (`schema_drift_lookback_days`), config hash unchanged since forward test began | Results from corrupted data or a tuned config are void. |

## Micro-live gate (S3; evaluator only)
Realized slippage <= 1.5x modeled; fill rate >= 80%; live ROI not significantly
below paper ROI; >= 100 live fills over >= 30 days; no kill switch triggered.

## Kill switches
- Settled drawdown from peak > 20% of stage bankroll: halt, manual review.
- Rolling-50-trade ROI below the bootstrap-implied 1st percentile: halt.
- Realized slippage > 2x modeled for 3 consecutive days: halt.
- Selected-wallet pooled OOS Z < 0 for 2 consecutive epochs: halt, re-run selection.
- Any data-integrity failure: halt.

## Changelog
| config_version | date | change | reason | author |
|---|---|---|---|---|
| 1 | 2026-10-05 | initial thresholds from the build spec | baseline, set before any evaluation | initial build |
