# CompoundBot

A paper-first, lower-volatility, multi-asset trading research and execution system. It aims at **small net wins that
survive realistic fees, spread and slippage**, and it treats "no edge found" as a valid result.

- Diversified ETFs plus an optional capped crypto-majors sleeve, daily bars only, no leverage, no shorting.
- Strategies: trend + volatility targeting, momentum rotation, short-term mean reversion, crypto trend. Each must
  pass the pre-registered evidence gate (`docs/GATE.md`) on **real data** before it is kept.
- Hard risk limits in `config/risk.yaml` (hash-checked), drawdown ladder, kill switch, order caps, data-health block.
- **No real-money code.** `LiveBroker` raises, and `tests/unit/test_safety.py` scans the source.
- Built from `docs/MASTER_PROMPT.md` (v3). Every build step is logged in `BUILD_LOG.md`.

## Status: infrastructure done, real-data research NOT done

The build sandbox had no market-data access (see `docs/ASSUMPTIONS.md`). Everything is tested on fixtures and a seeded
SYNTHETIC generator. **Nothing here shows that any strategy makes money on real markets.** Gate 1 (strategy
selection) is waiting for you to run the research on real data (`docs/RUNBOOK.md`, section 1).

| Step | State |
|---|---|
| 0-2 bootstrap, config, data layer | done (Alpaca data adapter UNVERIFIED; CSV importer is the reliable route) |
| 3-5 backtest engine, statistics, baselines | done, tested (lookahead, hand-computed equity, exact cost deltas, determinism) |
| 6-8 strategies, walk-forward harness, portfolio | done; harness validated on SYNTHETIC worlds (`docs/results/harness_acceptance.md`) |
| 9-11 risk, brokers, daily runner | done; Alpaca paper adapter UNVERIFIED (mocked-HTTP tests only) |
| 12 ML meta-model | skipped on purpose (too little data; see master prompt) |
| 13-14 monitoring, weekly review, dashboard | done |
| 15 holdout + 60-day paper soak | commands exist (`holdout`, `paper`, `gate`); not run: needs real data and time |
| 16 go-live prep | checklist only (`docs/RUNBOOK.md` s.4); no real-money adapter by design |

## Quick start
```bash
python -m venv .venv && . .venv/bin/activate
make setup && make test            # about 1 minute
python -m bot.cli data --source synthetic --start 2005-01-03 --end 2022-12-30   # SYNTHETIC demo data
python -m bot.cli research --placebo 20 --boot 200                               # ~2 min, reports/ (SYNTHETIC)
python -m bot.cli dash                                                           # http://127.0.0.1:8765
```
For real use follow `docs/RUNBOOK.md`: drop CSVs into `data/import/`, run `data --source csv`, `research`, review,
`select --approve "<you>"`, `backtest`, `holdout`, then schedule `nightly`.

## Layout
```
config/      universe, costs, risk (hash-checked), strategy grids (pre-registered), schedule
src/bot/     data/ backtest/ strategies/ portfolio/ risk/ broker/ run/ monitor/ dashboard/ cli.py
docs/        MASTER_PROMPT, ASSUMPTIONS, DECISIONS, GATE, RUNBOOK, SOURCES, results/
tests/       unit/ property/ (hypothesis fuzz) integration/
```
This is engineering scaffolding, not financial advice.
