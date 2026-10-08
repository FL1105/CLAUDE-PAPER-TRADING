# Final deliverable summary (build spec section 14)

Built: Phases 1 to 6 (all required phases). Phase 7 (optional `btc_lag`) not started.
Repository: `FL1105/CLAUDE-PAPER-TRADING`, branch `master`.

## 1. Test results
- **205 tests pass**, 1 skipped (it validates the adapters against live API responses
  and turns on automatically once `polybot probe` has saved them). Plus 1 slow
  test deselected by default (`pytest -m slow`: full 300-seed calibration).
- `ruff` clean; `mypy --strict` clean on `stats/`, `gates/`, `execution/`.
- Coverage 94% overall; stats/ 100%, gates/ 99 to 100%, execution/ 97 to 100%.
- Property tests (Hypothesis): order-book walker (never beats the best ask,
  shares x price = cost, never overspends) and Benjamini-Hochberg (monotone,
  q-controlled under the null).

## 2. Measured false-promotion rate and power (synthetic, full pipeline, all gates)
| world | promoted | rate (Wilson 95%) |
|---|---|---|
| no skill, 300 seeds | 0 | **0.0% (0 to 1.26%)**; target <= 1% |
| no skill, miscalibrated prices, 300 seeds | 0 | 0.0% (0 to 1.26%) |
| skilled, default strength | 0 / 100 | 0% |
| skilled, strong, 1 c spread | 0 / 100 | 0%: slippage and stress gates |
| skilled, strong, 0.5 c spread | 60 / 100 | **60% (50 to 69%)** |
| skilled, strong, high latency | 0 / 100 | 0%, as required |

The spec's 80% power target is not met at default settings, and gates were not
loosened to meet it. Copy cost is the binding constraint. Details:
`docs/CHECKPOINT_PHASE5.md`. P-value calibration and the selection-bias demo:
`docs/CHECKPOINT_PHASE2.md`.

## 3. Real API limitations discovered
Update 2026-10-06: `polybot probe` ran against the real API (13 steps, no failures, no
schema drift; findings in `docs/ASSUMPTIONS.md`). Discovered since the first build:
- **`/trades` offset cap.** The global feed stops at about 3,500 rows (about 7 minutes of
  trades), so `backfill --days 90` cannot reach 90 days. Real history comes from the
  Hugging Face dataset instead (`docs/HISTORY_IMPORT.md`, `polybot history import|markets`).
- **Still unverified:** the fee formula and coefficient, real spreads and depth, rate limits.
  Backtests keep using the guessed 1 c / $250-per-level book until the forward run records
  real books.

The text below is the original build-time statement and is kept for the record.

> The build sandbox's network policy blocked every `*.polymarket.com` host, so no endpoint
> was ever called. Everything about the API was an explicitly labeled assumption.

## 4. Verified vs not verified
| verified (by tests, here) | NOT verified |
|---|---|
| p-value calibration in a null world; BH FDR control | any real API schema or limit |
| leakage safety (future-mutation test, canary, injected-leak check) | the fee formula, coefficient and fee categories |
| book walking, fees math, chase rule, risk limits, settlement, crash recovery | real spreads and depth (backtests use a guessed 1 c / $250-per-level book) |
| fast engine == full replay (placebo and stress use it) | public dataset column layouts (`polybot history inspect` first) |
| forward loop end-to-end against a fake API | whether any real wallet has copyable edge |
| dashboard: offline, no network requests, phone layout, colorblind-safe colors | behaviour over weeks of real uptime |

## 5. Exact commands to start the forward paper run
```bash
git clone https://github.com/FL1105/CLAUDE-PAPER-TRADING.git
cd CLAUDE-PAPER-TRADING
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                   # expect all green

polybot probe                            # 1. verify the real API (exit 3 = schema drift: fix first)
pytest                                   #    adapters now also checked against real responses
polybot backfill --days 90               # 2. history from the API (resumable)
#   optional deeper history: see docs/HISTORY_IMPORT.md
#   polybot history inspect PATH ; polybot history import PATH --name SRC --start ... --end ...
#   polybot backfill --markets-only
polybot backtest --days 90               # 3. OOS backtest: tier Common at most
polybot gates --explain

polybot paper                            # 4. THE forward run: leave it running 30+ days
                                         #    (tmux / launchd / systemd recipes: docs/RUNBOOK.md)
polybot report                           # 5. any time: out/report.json + open dashboard.html
```
Do not edit `config/default.yaml` once `polybot paper` has started. Gate 12 will
fail. A new config means a new database and a new forward run.

## 6. Notable things found and fixed during the build
Null-world spread bias (D11), hex IDs re-typed by CSV sniffing, unmerged
equal-price book levels (found by a property test, D23), end-only settlement
starving capacity (D22), trade-level losing streaks (D27), a numpy-bool
serialisation crash, and dashboard phone overflow. Each has a regression test
or a decision entry.
