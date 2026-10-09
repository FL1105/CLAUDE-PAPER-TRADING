# RUNBOOK

All commands run from `compoundbot/` inside a virtualenv (`make setup`). On Windows use `python -m bot.cli ...`
and Task Scheduler for the nightly job. Nothing here trades real money: the live adapter is a stub that raises.

## 1. First run on YOUR machine (this is where real research happens)
1. `python -m venv .venv && . .venv/bin/activate && make setup && make test`
2. Real data, either route:
   - CSV (most reliable): put one file per symbol in `data/import/` (`SPY.csv`, `BTC-USD.csv`, plus the cash proxy `BIL.csv`).
     Columns: `date, close`, optionally `open, high, low, volume, adj_close`. Free sources work (download from your broker or data vendor).
   - Alpaca data API: copy `.env.example` to `.env`, add PAPER keys, then verify `src/bot/data/providers.py::AlpacaDataProvider`
     against the current Alpaca docs first (it is UNVERIFIED) and record the URL and date in `docs/SOURCES.md`.
3. `python -m bot.cli data --source csv --start 2003-01-01` then read `reports/data_quality.md`. Fix or explain every finding.
4. `python -m bot.cli research` (about 10-30 minutes). Read `reports/baselines.md`, `reports/research_v1.md`.
5. **GATE 1 (yours):** decide which strategies to keep. `python -m bot.cli select --approve "<your name>"` writes
   `config/selected.yaml` from the KEEP verdicts. The runner refuses to trade until `approved_by` is filled.
6. `python -m bot.cli backtest` writes `reports/portfolio_v1.md` (vol vs target, sleeves, correlations, baselines).
7. `python -m bot.cli holdout` evaluates the frozen holdout exactly once. Do not re-tune after it.

## 2. Paper trading
- Local simulator (same cost model as the backtest): `python -m bot.cli paper` (state in `state/`).
- Alpaca paper: verify `src/bot/broker/alpaca_paper.py` against current docs, set keys in `.env`, run `python -m bot.cli paper --broker alpaca`.
- Schedule `python -m bot.cli nightly` once per NYSE session after 16:10 New York time:
  - cron (Linux/macOS, server in New York time): `10 16 * * 1-5 cd /path/compoundbot && .venv/bin/python -m bot.cli nightly`
  - Windows: Task Scheduler, daily 16:10 Eastern, action `python -m bot.cli nightly`, start in the project folder.
- Set `ALERT_WEBHOOK_URL` in `.env` for failure alerts. `state/heartbeat` is touched on every run; alert if older than 30 hours.
- Open the dashboard: `python -m bot.cli dash` then http://127.0.0.1:8765 . The kill switch needs the token in `state/dash_token`.

## 3. Operations
| Situation | Action |
|---|---|
| Stop everything now | `python -m bot.cli kill --reason "why"`, or the dashboard button. Orders are blocked before the next submit. |
| Resume | `python -m bot.cli release --by "<your name>"` (only after you understand why it was engaged). |
| Drawdown HALT (-15%) | The runner flattens (sells only), then engages the kill switch. Review, then release and delete the ladder state decision yourself: edit `state/state.json` `ladder_level` to `HALF` and set `ladder_peak` to current equity. |
| Run crashed | Re-run `nightly`. A stale lock (> 6 h) is cleared automatically; orders use deterministic ids so nothing double-submits; targets are re-derived from current data, so missed days heal. |
| Missed run | Same as above. |
| Unreconciled positions | The run record lists them under `mismatches`. Stop (kill), compare with the broker UI, fix, release. |
| Data stale/unhealthy | The runner blocks all orders (`status: data_unhealthy`). Fix the feed; do not override. |
| Market halted / broker down | Orders reject or error; the run is logged and an alert fires. Do nothing manual unless a position must be closed; use the broker UI. |
| Rotate keys | Revoke in the broker dashboard, create new paper keys, update `.env`. Never commit `.env`. |
| Restore from backup | Back up `state/`, `runs/`, `reports/`, `config/` and `docs/risk_ack.txt`. Restore the folders, run `python -m bot.cli check`, then `nightly`. |
| Changed `config/risk.yaml` | You must re-sign: `sha256sum config/risk.yaml` into `docs/risk_ack.txt`. Nothing automated may do this. |

## 4. Go-live checklist (NOT implemented; for GATE 3)
A real-money adapter does not exist on purpose. Before anyone writes one, all of this must hold, and the adapter
must satisfy it in code:
- `python -m bot.cli gate` shows ALL PASS on MEASURED research data and the paper soak.
- Operator has read `reports/holdout.md`, `reports/research_v1.md` and `docs/GATE.md`.
- Adapter requires `LIVE_ENABLED=true` set by hand, a pre-trade confirmation file created by hand each day,
  per-order and per-day notional caps below the paper caps, and a startup assertion on the account id.
- First stage: an amount you can fully afford to lose, plain cash account, ETFs and liquid crypto, no leverage/short/options.
  Run at least a quarter. Raise capital at most 2x per step, at least a month apart, only if live tracking error and
  measured fees/slippage still support the cost-cover rule (gross edge >= 2x round-trip cost).
- Check short-term gain and wash-sale treatment with a qualified professional.
