# ASSUMPTIONS

## Environment preflight (2026-10-09, build sandbox)
| Host | Result |
|---|---|
| query1.finance.yahoo.com | BLOCKED (proxy 403 on CONNECT) |
| stooq.com | BLOCKED |
| paper-api.alpaca.markets / data.alpaca.markets | BLOCKED |
| api.coinbase.com / api.exchange.coinbase.com | BLOCKED |
| fred.stlouisfed.org | BLOCKED |
| pypi.org | reachable |

Consequence: no real market data and no broker round trip could be run during the build.
Everything is built and tested against fixtures, CSV import and the seeded SYNTHETIC generator.
No research verdict on real markets exists yet. Gate 1 is WAITING ON REAL DATA.
Python 3.13 in a venv; pandas 3.x, numpy 2.x, scipy, scikit-learn, pydantic 2.

## Layout
- Package is `bot` under `compoundbot/src/` (per the master prompt). The project lives in a
  subfolder because the repo root already holds the unrelated `polybot` project.
- Cash leftover earns the cash-proxy (BIL) return in backtests. In paper/live the runner can park
  residual cash in BIL (`park_cash_in_bil`); broker cash itself earns nothing.

## Modelling choices
- Backtest timing: weights computed from data through the close of day t; execution mode
  `next_open` (default) or `next_close`. Old weights earn the overnight gap, new weights earn the session.
- Crypto in the synthetic world is generated on the equity trading-day calendar with vol scaled
  by sqrt(365/252). Real crypto data must be aligned to the same decision time (16:00 New York).
- Annualisation: 252 for the book; crypto weekend returns compound into the next equity day.
- NYSE holidays come from a built-in rule set (calendar.py); pandas_market_calendars is not required.
- Real broker data/API field names are NOT verified (network blocked). `alpaca_*` modules are marked
  UNVERIFIED and must be checked against current docs before the first real paper run.

## Execution timing (paper runner)
- The runner executes after the close using closing data. Orders are queued and fill at the NEXT session's open
  (`PaperLocalBroker(execution="next_open")`), matching the backtest's `next_open` mode. Queued orders from a missed
  session expire instead of filling late. A kill switch cancels waiting buys but lets risk-reducing sells settle.
- Trade days: weekly (last NYSE session of the week) unless a daily sleeve (mean reversion) is selected. A change in
  the drawdown-ladder multiplier forces a rebalance. Same rule in the backtest and the runner.
- Orders use limit prices 90% of the way to the 3% price collar (effectively market-on-open with a sanity bound).
- Paper fills are idealised; paper-vs-replay tracking error (measured on SYNTHETIC data: ~5 bps daily) tests plumbing,
  not real slippage.

## Synthetic worlds
`planted_edge` worlds use edge_strength=4 (equity drift regimes of +/-40%/yr). That is far stronger than reality and
makes SYNTHETIC baselines look odd (e.g. SPY drawdowns above 80%). It exists so the harness has something detectable.
