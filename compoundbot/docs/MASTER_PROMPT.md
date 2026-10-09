<prompt>

<goal>
Build CompoundBot: a systematic, lower-volatility trading system for steady compounding of SMALL NET WINS. Small is fine: a few cents or a few basis points per trade or per day is a perfectly good result, as long as it is still positive AFTER realistic fees, spread and slippage, and as long as the evidence says it repeats. Gross-looking edges that fees eat are the only thing rejected. It trades a diversified universe (SPY QQQ IWM EFA EEM TLT IEF LQD GLD DBC VNQ, cash proxy BIL) plus an optional crypto-majors sleeve (BTC/USD ETH/USD (max 10% of equity, can be disabled)), targets about 10% annualised volatility, uses no leverage or shorting at first, and cuts risk automatically in drawdowns.

It runs on PAPER money first (10000 USD via Alpaca paper trading (verify current API docs)). It becomes eligible for small real money only after it passes the evidence gate below AND the human operator says yes.

You (Claude Code) do the whole build yourself, step by step, without waiting for approval between steps. You only stop at the HUMAN GATES in <human_gates>. Be rigorous; the operator is learning the math and wants numbers, not hype.
</goal>

<ground_truth>
1. Small net edges compound. Per-day NET return to yearly (252 days): 0.02%/day = +5.2%, 0.05%/day = +13.4%, 0.10%/day = +28.6%. The operator already accounts for fees, so the job is to make the cost model honest, not to avoid small edges. Per-trade test: net edge = gross edge - round-trip cost (2 sides x per-side bps, plus spread and slippage). A sleeve is worth running only if its measured net edge is positive with a confidence interval AND covers its modelled cost at least 2x (cost-cover ratio = gross edge / round-trip cost >= 2), so a modest cost misestimate cannot erase it.
2. Fee shape matters at small size. Percent fees scale with notional; fixed fees, minimum order sizes and rounding do not. With 10000 USD, model per-order minimums, lot or fractional-share rounding, and the no-trade band explicitly. A 1 USD fee on a 30 USD profit is 3%, on a 3000 USD profit it is 0.03%.
3. Turnover is a cost line. Example: 3 bps per side and a book that turns over 10x per year (each turn = one sell + one buy, so 2 sides) costs 10 x 2 x 3 bps = 0.6%/yr. Higher turnover is allowed only when item 1 holds with margin.
4. Volatility drag: geometric growth g ~ mu - sigma^2/2. With mu = 12%: sigma 20% gives g = 10%, sigma 10% gives g = 11.5%. Caveat: mu is not free. Cutting volatility by holding more cash also cuts mu. Volatility targeting raises Sharpe and cuts drawdown only if the signals carry real edge; it does not turn a zero-edge book into a profitable one. Always compare against a vol-targeted buy-and-hold baseline to isolate the signal's contribution.
5. Drawdown asymmetry: a loss of d needs a gain of d/(1-d): -10% needs +11.1%, -20% needs +25%, -33% needs +50%, -50% needs +100%.
6. Sharpe is hard to prove. SE of an annualised Sharpe SR over T years is about sqrt((1 + SR^2/2)/T). For SR=1, one-sided 95% (z=1.645) needs T ~ 4 years, two-sided 95% (z=1.96) needs ~6 years. A 60-90 day paper run (SE ~ 1.7+) cannot prove an edge. So: the EDGE is argued from walk-forward history plus a frozen holdout; the PAPER period proves the implementation matches the backtest (fills, costs, bugs), not the edge.
7. How much history exists is limited. Most of the universe's ETFs launched 2002-2007, so real history is about 17-20 years with maybe 3-5 independent macro regimes. Do not claim 'decades'. Treat every result as low-power and say so. Higher-turnover sleeves get more observations, but their edge must be re-checked at x2 costs.
8. Realistic priors, to be tested not assumed: diversified trend / momentum / risk-parity style portfolios have historically shown after-cost Sharpe ratios of roughly 0.4-0.9. Above ~1.5 in a backtest means bug or leak until proven otherwise.
9. Win rate is irrelevant. Expectancy = p*W - (1-p)*L - cost is what matters. High-win-rate, small-win strategies (short-term mean reversion) hide fat left tails: always report the worst loss and loss distribution beside the win rate.

Design consequence: daily bars, low-to-moderate turnover, diversification, volatility targeting, hard drawdown control, an honest cost model with a cost-cover requirement, and statistics that respect how little independent data exists.
</ground_truth>

<rules>
1. No profitability claim without NET-of-cost, OUT-of-sample numbers and a confidence interval.
2. No lookahead. Signals use data up to the decision time on day t and trade AFTER it. A test must fail if any feature peeks forward.
3. Never invent API fields, fees, tickers or data. Verify against official docs; record URL and date in docs/SOURCES.md. If unverifiable, put it behind a flag, say so, and treat the number as ESTIMATED.
4. Hard risk limits live in code and config (risk.yaml, hashed at startup). No model, proposal file or nightly job may edit them.
5. Default mode is PAPER. LIVE is a separate adapter that only raises NotEnabled. Do NOT write real-money order submission or key-signing code in this build; a test scans the source to enforce it.
6. Secrets only in .env (gitignored) or the OS keychain. Never in the repo, logs or any prompt. Paper keys only until the operator says otherwise.
7. Every strategy variant tried is appended to experiments.jsonl with its Sharpe, because deflated statistics need the trial count AND the variance of Sharpe across trials.
8. Label results MEASURED (real data) / SYNTHETIC (generated data, shows code works, proves nothing about markets) / ESTIMATED / SPECULATION. Never present SYNTHETIC numbers as evidence. Report negative results as clearly as positive ones. A strategy that fails the tests is deleted, not tuned until it passes.
9. Daily or slower bars only for the core. No intraday day-trading (avoids pattern-day-trader limits and microstructure noise). Verify account-type rules in current broker docs.
10. Cost model first. Every report shows gross AND net, with the cost assumptions printed at the top.
</rules>

<environment>
The build machine may have NO market-data network access (a sandbox often allows only package indexes). Check before assuming:
  - Try one tiny request to each data host you plan to use (broker data API, the free daily-bar fallback, a Treasury/cash-rate source). Record reachable / blocked in docs/ASSUMPTIONS.md.
  - If blocked: still build everything. Make every provider swappable and add (a) a CSV/parquet import path (drop files into data/import/), (b) fixtures, and (c) a seeded SYNTHETIC generator (correlated assets, volatility clustering, regimes, a planted trend edge and a null world with none) so the full pipeline and its statistics can be tested end to end.
  - Without real data, research verdicts cannot be produced. Build all infrastructure, mark Gate 1 as 'waiting on real data', write the exact commands the operator runs on their own machine (make data && make research), and do not invent results.
  - Also record Python version and which packages installed. Pin what works.
</environment>

<autonomy>
- Execute the BUILD STEPS in order, without asking permission between steps. After each step: run tests, commit with a clear message, append a short entry to BUILD_LOG.md (built, measured, unverified), then continue.
- If something is ambiguous, choose the most reasonable option, write it to docs/ASSUMPTIONS.md, and keep going. Ask the operator only if truly blocked (credentials, paid data, legal/account question). One question at a time.
- If a step fails its acceptance test, fix it before moving on. If you cannot, log the blocker and move to the next independent step.
- Prefer small, boring, well-tested code. Pure functions for signals, risk and sizing. Type hints and docstrings.
- Keep everything reproducible: pinned dependencies, seeded RNG, config hash and git commit recorded with every backtest and every order.
- The trading path itself uses NO LLM calls. Stay under a small daily LLM budget (about $5) for any optional review tooling.
- Begin by printing a 10-line plan, then start the preflight and Step 0 immediately.
</autonomy>

<stack>
Python 3.11+, venv or uv, pandas, numpy, scipy, pydantic, pyyaml, pyarrow, pandas_market_calendars (optional; fall back to a built-in NYSE holiday rule set), fastapi (optional dashboard API), httpx, pytest, hypothesis, ruff. scikit-learn / lightgbm only if Step 12 is enabled. Broker SDK: whichever Alpaca paper trading (verify current API docs) officially documents (verify package and version before installing); prefer plain httpx against documented REST endpoints to keep the dependency small.

compoundbot/            (subfolder of the repo if the repo already holds another project; keep its own pyproject and tests)
  CLAUDE.md  BUILD_LOG.md  Makefile  pyproject.toml  .env.example  .gitignore
  config/   universe.yaml  costs.yaml  risk.yaml  strategy.yaml  schedule.yaml
  src/bot/
    data/        providers.py cache.py quality.py calendar.py synthetic.py
    backtest/    engine.py costs.py metrics.py stats.py walkforward.py report.py
    strategies/  trend.py momentum_rotation.py mean_reversion.py crypto_trend.py
    portfolio/   construct.py vol_target.py
    risk/        limits.py drawdown.py killswitch.py
    broker/      base.py paper_local.py alpaca_paper.py live_stub.py
    run/         daily.py reconcile.py scheduler.py
    ml/          (optional, Step 12)
    monitor/     drift.py weekly_review.py
    dashboard/   api.py static/index.html
  tests/        unit/  property/  integration/
  data/ (gitignored)  runs/<date>/  reports/  docs/ (SOURCES ASSUMPTIONS DECISIONS GATE RUNBOOK)
</stack>

<conventions>
- One decision time per day: after the US equity close (16:00 America/New_York). Crypto uses its price at that same instant, so all assets share one timeline. Orders for equities go in for the next open (or next close, configurable); crypto orders can go immediately.
- Annualisation: 252 for equities and cash, 365 for crypto. When mixing, compute portfolio returns on the shared equity-trading-day calendar and compound the crypto weekend moves into the next equity day. Document it.
- Returns are TOTAL returns (dividends reinvested). Use adjusted prices consistently for signals and P&L; if trading at the open, adjust the open with the same factor. Store raw and adjusted.
- Sharpe and Sortino use returns in excess of the cash proxy (BIL). Say which one each table uses.
- The cash proxy is held as an asset and is excluded from volatility scaling (its tiny vol would blow up inverse-vol weights).
- Sizing: with 10000 USD use fractional shares if the broker supports them, otherwise round down and carry the residual as cash; apply the no-trade band and a minimum trade size so crumbs do not generate fee-eaten orders.
- Backtests must include the drawdown ladder and halts (see <risk_layer>), including the re-arm rule, or reported drawdowns are optimistic.
</conventions>

<strategies>
Each is a hypothesis implemented as a pure function: prices -> target weights. Test on history. Keep only what survives. Do not assume any works.

A. Trend + volatility targeting (core). For each asset: ON if close > SMA(200) (also test 150 and 250, and the sign of 12-1 month momentum). Among ON assets compute inverse-volatility weights (60-day realised vol). Scale the whole book so estimated portfolio vol (shrunk covariance) equals 10%, cap gross at 100% and each asset at 25%, and put the unused budget in BIL. Rebalance weekly or monthly with a 1% no-trade band.
B. Cross-sectional momentum rotation. Monthly: rank by 6-12 month return skipping the latest month; hold the top N (3-5) equal-risk weighted, only those with positive absolute momentum.
C. Short-term mean reversion (the 'small frequent wins' candidate). Long only, liquid ETFs above SMA(200): enter when RSI(2) < 10, exit when RSI(2) > 60 or after 5 days, stop at 2.5x ATR(14). Per-trade net edge must beat the cost-cover rule in <ground_truth> at x1 and x2 costs. Report worst loss, loss distribution, and average net bps per trade. Keep only if it passes.
D. Crypto majors trend. BTC and ETH: hold when close > SMA(100) or SMA(200) AND realised vol below a cap, vol-targeted, sleeve capped by CRYPTO_SLEEVE. Use the crypto cost (25 bps per side) plus spread; verify the broker's crypto fee tier.
E. Defensive sleeve. BIL / short Treasuries when the risk budget is unused. This is the 'off' state, not a strategy.
F. High-risk sleeve (memecoins etc): OUT OF SCOPE for this build.

Baselines every strategy must beat on a RISK-ADJUSTED basis: buy-and-hold SPY, 60/40 (SPY/IEF), equal-weight universe, cash, AND vol-targeted versions of equal-weight and 60/40 (same risk scaling, no signal). Also run a PLACEBO: the same strategy with its signals randomly shuffled in time (same turnover, same exposure distribution), 500+ draws. The real strategy must beat the placebo distribution (p < 0.10).
</strategies>

<step_0_2>
STEP 0 - Bootstrap.
  - Create the project folder, venv, pyproject with pinned deps, ruff, pytest, .gitignore (.env, data/, runs/, __pycache__), .env.example (key NAMES only).
  - Write CLAUDE.md from <rules> and <autonomy>. Create BUILD_LOG.md and docs/*.md stubs. Run the <environment> preflight and record it.
  - Makefile targets: setup, test, data, research, backtest, paper, dash, nightly.
  - ACCEPT: 'make setup && make test' passes with at least one trivial test.

STEP 1 - Config and schemas.
  - pydantic models (extra='forbid') + YAML: universe.yaml (symbols, asset class, inception date), costs.yaml (per-side bps ETF 3 / crypto 25, spread/slippage bps, commission, fixed fee, min order USD), risk.yaml, strategy.yaml (parameter grids, pre-registered), schedule.yaml.
  - Compute and store a SHA-256 of risk.yaml; refuse to start paper mode if it changed without an operator-signed bump (docs/risk_ack.txt containing the new hash).
  - ACCEPT: tests load every config, reject bad values and unknown keys, and detect a changed hash.

STEP 2 - Data layer.
  - providers.py: abstract DataProvider; implementations: broker data API (verify), free daily-bar fallback (verify), CSV/parquet importer, and SYNTHETIC. Selected by config.
  - cache.py: parquet files data/bars/<symbol>.parquet, incremental update, atomic writes.
  - calendar.py: exchange calendar for equities; 24/7 days for crypto.
  - quality.py: missing sessions, zero/negative prices, split-like jumps, stale bars, volume anomalies, cross-source disagreement. Writes reports/data_quality.md.
  - synthetic.py: seeded generator for the whole universe (correlated returns, GARCH-like vol clustering, regime switches), with switches 'planted_edge' (trend) and 'null' (none). Used by tests and by the pipeline when real data is unavailable. Output is always tagged SYNTHETIC.
  - Bias note: ETFs avoid most survivorship bias; do not backtest an asset before its inception date. List remaining biases in docs/ASSUMPTIONS.md.
  - ACCEPT: 'make data' builds the cache from real data when reachable, otherwise from import/synthetic with a clear banner; quality report has zero unexplained gaps; tests use a small fixture.
</step_0_2>

<step_3_5>
STEP 3 - Backtest engine (daily, honest).
  - engine.py: price panel + weight function -> daily weights, trades, equity. Weights use data through the decision time of day t, execute at the OPEN (or next close, configurable) of day t+1. Never same-bar.
  - costs.py: per-side bps + spread/slippage + commission + fixed fee + minimum order, crypto separate, cash earns the BIL return. Track turnover, trade count, average net bps per trade, and total cost drag per year.
  - Include the no-trade band, minimum trade size, and the drawdown ladder with its re-arm rule.
  - TESTS: (a) lookahead: perturb all data after day t and assert weights up to t are unchanged; (b) known-answer synthetic series with hand-computable equity; (c) zero-cost vs cost run differ by exactly the modelled amount; (d) determinism.
  - ACCEPT: all four pass.

STEP 4 - Metrics and statistics.
  - metrics.py: CAGR, ann. vol, Sharpe, Sortino, max drawdown + duration, Calmar, ulcer index, worst day/month/year, hit rate, profit factor, expectancy, avg net bps per trade, cost-cover ratio, turnover, cost drag.
  - stats.py: stationary block bootstrap CIs (block length chosen by Politis-White or ~10-20 days), Probabilistic Sharpe Ratio, Deflated Sharpe Ratio using trial count AND Sharpe variance across trials from experiments.jsonl, minimum-track-record length, paired bootstrap on strategy-minus-benchmark, shuffled-signal placebo test.
  - ACCEPT: unit tests against hand-computed values and published formulas; everything seeded.

STEP 5 - Baselines.
  - Implement buy-and-hold SPY, 60/40, equal-weight, cash, vol-targeted equal-weight and vol-targeted 60/40. Run over full history and sub-periods.
  - reports/baselines.md with all metrics and CIs, tagged MEASURED or SYNTHETIC.
  - ACCEPT: report generated; on real data, SPY figures sanity-checked against well-known long-run numbers (ballpark).
</step_3_5>

<step_6_8>
STEP 6 - Implement strategies A-E as pure functions. ACCEPT: unit tests on synthetic trending / mean-reverting series confirm behaviour; lookahead test passes for each.

STEP 7 - Walk-forward research harness.
  - Expanding-window walk-forward. Parameter grids come from strategy.yaml and are PRE-REGISTERED and small. Each fit window picks parameters; the next window is evaluated untouched. Log every trial to experiments.jsonl.
  - FREEZE the last 2 years as a holdout. Do not run any strategy on it until Step 15. If you accidentally do, log it in DECISIONS.md and tell the operator. Note the holdout is low-power (SE of Sharpe ~ 0.7 over 2 years): it is a sanity check against overfitting, not proof.
  - Per strategy, out-of-sample: net Sharpe + 95% CI, PSR, deflated Sharpe, max drawdown, worst year, turnover, cost drag, cost-cover ratio, average net bps per trade, vs baselines and placebo, at cost x1 and x2, and per regime (2008-09, 2013 taper, 2015-16, 2018 Q4, 2020 crash, 2022 rates; only those inside the available data).
  - Parameter robustness: tabulate Sharpe over neighbouring values; prefer a plateau to a spike.
  - Automated verdict per strategy using <evidence_gate>: KEEP / REJECT with reasons, in reports/research_v1.md. REJECTED strategies are removed, not re-tuned.
  - When running on SYNTHETIC data, the verdict table must carry a banner saying it validates code only. Also run the planted-edge and null worlds: the harness must KEEP the planted edge and REJECT the null at the expected rates. That is the harness's own acceptance test.
  - ACCEPT: report exists; verdict logic automated; null-world false-KEEP rate is within tolerance.

STEP 8 - Portfolio construction.
  - construct.py: combine KEPT strategies by inverse-volatility risk budgeting with a Ledoit-Wolf shrunk covariance. vol_target.py scales the book to 10% using a 60-day estimate, leverage cap 1.0, MAX_ASSET_PCT per asset, turnover penalty / no-trade band.
  - Report combined portfolio vs baselines, contribution by sleeve, correlation matrix, realised vs target volatility.
  - ACCEPT: realised vol within about +/-25% of target out-of-sample; leverage never exceeds 1.0; report generated.
</step_6_8>

<step_9_11>
STEP 9 - Risk layer. Pure functions, near-100% branch coverage. Limits from risk.yaml:
  - max weight per asset 25%; gross <= 100%; crypto sleeve cap; no shorts, leverage or options.
  - Drawdown ladder, rules in <risk_layer>, including explicit re-risk and re-arm rules.
  - Daily loss halt; order sanity checks (reject an order above max_weight + 5% of equity, above 1% of the asset's ADV, or priced > 3% from the last quote).
  - Kill switch: file flag, CLI command, and a dashboard control; checked before EVERY order; state persists across restarts.
  - TESTS: hypothesis fuzz (random weights, NaNs, stale prices, huge numbers); no order may break any limit.
  - ACCEPT: fuzz passes; the kill-switch drill is an automated test.

STEP 10 - Broker adapters.
  - base.py: abstract Broker (get_account, get_positions, get_quote, submit_order(client_order_id), cancel, get_order).
  - paper_local.py: local simulator using the SAME cost model as the backtest.
  - alpaca_paper.py (or Alpaca paper trading (verify current API docs)): real PAPER REST API, keys from .env, base URL pinned to the documented paper endpoint, hard assertion at startup. Verify every field name from docs. Note that paper fills are idealised, so paper results validate plumbing, not execution quality.
  - live_stub.py: raises NotEnabled. No real-money code.
  - Order logic: diff target vs current; fractional or rounded lots; sell first then buy; marketable-limit with a price collar; idempotent client_order_id; reconcile fills; handle closed market, halts, rejects, partial fills.
  - ACCEPT: tests with mocked HTTP cover every branch; one real PAPER round trip if keys and network exist, else logged as pending.

STEP 11 - Daily runner and scheduler.
  - run/daily.py (idempotent, lock file, structured logs): refresh data -> quality check -> signals -> weights -> risk checks -> orders -> submit -> reconcile -> write runs/<date>/ (inputs hash, weights, orders, fills, equity).
  - Scheduled after the US close via cron / launchd / Task Scheduler / apscheduler. Heartbeat file; alert on failure or missed run.
  - ACCEPT: a dry run on the last 30 historical days reproduces the backtest weights exactly; a missed run recovers cleanly.
</step_9_11>

<step_12_14>
STEP 12 - Optional ML meta-model. DEFAULT: SKIP. Roughly 18 years of daily data across a dozen correlated assets is very little for gradient boosting; enable only after Steps 7-8 give a validated baseline and the operator asks. If enabled: meta-labeling LightGBM predicting whether each signal's next-N-day net return is positive, purged K-fold with embargo, probabilities calibrated, output used only to scale size in [0,1]. KEEP only if out-of-sample Sharpe or max drawdown improves with a bootstrap CI excluding zero after counting every trial and at x2 costs; otherwise delete the module.

STEP 13 - Monitoring and weekly review.
  - drift.py: rolling 60-day realised Sharpe/vol vs the backtest-expected band; paper-vs-backtest tracking error; data-quality trends. Below the band for a pre-set period -> auto-switch to the cash sleeve and alert.
  - weekly_review.py writes reports/weekly_<date>.md (performance vs benchmarks, risk state, cost drag, tracking error, changes). An LLM may write the narrative and propose up to 3 hypotheses; proposals are queued, tested through Step 7, and can NEVER edit risk.yaml, the gate, or enable live trading.
  - A challenger replaces the champion at most monthly, and only if the lower 95% bound of the out-of-sample improvement is > 0 at x1 and x2 costs.

STEP 14 - Dashboard (dark, localhost only, mobile friendly).
  - Read-only JSON API plus one static page. Panels: equity vs SPY / 60-40 / backtest replay, drawdown, current vs target weights, sleeve contributions, searchable trade log, rolling Sharpe/vol vs expected band, gross vs net and cost drag, average net bps per trade, risk state (ladder, kill switch, data health), evidence-gate checklist with progress.
  - Controls: kill switch, pause entries, export CSV. These are the only write actions, they can only REDUCE risk, and they require a local token. Nothing may change risk limits or enable live trading.
  - ACCEPT: page renders from real or synthetic run data (banner says which); kill switch works in a test.
</step_12_14>

<step_15_16>
STEP 15 - Holdout test and paper soak.
  - Run the final chosen portfolio ONCE on the frozen holdout. Record reports/holdout.md exactly as it comes out. No re-tuning afterwards.
  - Start the scheduled paper run for 60 trading days with no manual parameter edits. Each week append a paper-vs-backtest-replay comparison.
  - Write docs/GATE.md with the final pass/fail table.

STEP 16 - Go-live preparation (documents and interface only; do NOT write or enable real-money submission).
  - Define the interface and the checklist a real-money adapter must satisfy: pre-trade confirmation file created by hand, per-order and per-day notional caps, startup assertion on account identity. Leave the adapter itself unimplemented.
  - Write docs/RUNBOOK.md: start, stop, kill, recover, reconcile, rotate keys, restore from backup, halted market, broker down.
  - Prepare the staged-capital plan in docs/GATE.md. Then STOP and report (human gate 3).
</step_15_16>

<risk_layer>
max_weight_per_asset: 25%
crypto_sleeve_max: 10% of equity (0 disables)
gross_exposure_max: 100%   shorts: off   leverage: off   options: off
target_vol_annual: 10%   vol_window: 60 trading days
drawdown_ladder (from peak equity): -8% halve all risk; -12% move to the cash proxy; -15% HALT, flatten, require manual re-arm.
re-risk rules (needed, or the ladder whipsaws): after the -8% step, restore full risk only once equity recovers to within 4% of peak OR 40 trading days pass AND the trend filter is ON for a majority of assets. After the -12% step, step back to half risk only after 20 trading days with positive trend breadth. In backtests the HALT is re-armed automatically after 60 trading days (report this assumption); in live it is manual only.
daily_loss_halt: 3% of start-of-day equity
order_caps: <= max_weight + 5% of equity per order, <= 1% of ADV, price collar 3%
rebalance: weekly (core) / monthly (rotation), no-trade band 1% weight, minimum trade 25 USD
data_health: block all orders if any required symbol's latest bar is stale or fails quality checks
</risk_layer>

<evidence_gate>
A Sharpe-1 edge needs ~4-6 years to prove, so the gate separates EDGE evidence (history) from IMPLEMENTATION evidence (paper). All must pass. All numbers must come from real data (MEASURED); SYNTHETIC runs can only test the gate logic.

EDGE (walk-forward out-of-sample + frozen holdout):
E1. Net Sharpe 95% block-bootstrap lower bound > 0, and Deflated Sharpe >= 0.90 given trial count and Sharpe variance.
E2. Beats the best risk-adjusted baseline, INCLUDING the vol-targeted baselines (paired bootstrap p < 0.10), or matches its return with clearly lower max drawdown. Beats the shuffled-signal placebo (p < 0.10).
E3. Net Sharpe still positive at cost x2, and cost-cover ratio >= 2 for any sleeve whose turnover exceeds 12 full-book turns per year.
E4. Out-of-sample max drawdown, WITH the ladder active, <= 15% + 5 points; worst calendar year not worse than -10%. The live HALT stays at -15%.
E5. Parameter plateau: neighbours (+/-20%) keep Sharpe within 30% of the chosen one.
E6. No single year contributes more than 40% of total OOS return; at least 3 regimes non-negative or near flat (fewer if the data lacks them; say so).
E7. Holdout consistency: holdout net Sharpe is positive and its 95% CI overlaps the walk-forward estimate. Do not use a hard 'Sharpe drop < 50%' rule; it is statistically meaningless over 2 years.

IMPLEMENTATION (paper):
I1. 60 trading days with zero manual intervention, zero unreconciled positions, zero risk-limit breaches.
I2. Paper equity vs backtest replay on the same days: daily tracking error below 15 bps and cumulative cost drag within 25% of modelled. Caveat: paper fills are idealised, so this tests plumbing and sizing, not real slippage.
I3. Kill-switch, drawdown-halt and crash-recovery drills pass.
I4. Realised vol within +/-25% of target over the paper period (a 60-day vol estimate has ~10% relative error itself; report the CI).

Report each criterion as value / threshold / PASS-FAIL / CI / sample size. A FAIL is a legitimate result: write down why and what to test next. Do not loosen a threshold after seeing results.
</evidence_gate>

<human_gates>
GATE 1 - After Step 8: show reports/research_v1.md and the portfolio report. If real data was unavailable, say so plainly, state that research is NOT done, list the operator commands to run, and keep building the independent steps (9-11, 13, 14). Wait for the operator to confirm which strategies to keep (default: the automated verdicts on real data).
GATE 2 - After Step 15: show holdout.md and GATE.md. Wait.
GATE 3 - After Step 16: the operator decides about real money. You never enable LIVE yourself.

Outside these gates, keep building. Missing credentials or a paid data need are blockers: ask once, then continue with independent steps.
</human_gates>

<go_live>
- Stage 0: paper (default).
- Stage 1: a small real amount the operator can fully afford to lose, plain cash account, ETFs and liquid crypto only, no leverage, no shorting, no options. Run at least a quarter before changing anything.
- Stage 2: raise capital at most 2x per step, at least a month apart, only if real results still track the backtest replay. Re-measure real fees and slippage first: if real net edge per trade is below the cost-cover threshold, stop.
- Remind the operator: past performance does not guarantee future returns; real fills, taxes (short-term gains, wash-sale rules) and behaviour in drawdown differ from paper. Suggest checking tax treatment with a qualified professional.
- Auto-demote to paper/cash on: kill switch, drawdown halt, data unhealthy, tracking error breach, or broker/account mismatch.
</go_live>

<final_check>
- Could any feature or signal use information from after the decision time?
- Is the edge still there at x2 costs, with a neighbouring parameter, and without the best single year?
- Net edge per trade vs round-trip cost: does the cost-cover ratio hold?
- How many trials did I run, and did the deflated statistics account for them?
- Did I touch the holdout early?
- Are these numbers MEASURED or SYNTHETIC, and is that label on every table?
- Does realised volatility match the target, and is leverage really capped at 1.0?
- Is any hard limit delegated to a model instead of code?
- Am I reporting gross or optimistic numbers as a headline?

End every report with a section titled 'WHAT COULD I BE WRONG ABOUT?' and prefer a system that survives over one that merely looks profitable.
</final_check>

<kickoff>
Begin now. Print a 10-line plan for the preflight and Steps 0 to 16, then start immediately without asking for confirmation. Work through the steps in order, committing after each, logging to BUILD_LOG.md, and stopping only at the three human gates. Start.
</kickoff>

</prompt>
