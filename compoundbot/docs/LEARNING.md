# LEARNING: how the bot improves without fooling itself

"Improve forever" is not possible with about 250 daily bars a year: every new variant tested on the same history
spends some of the evidence, and enough variants will always produce a lucky winner. So the loop is slow, counts
everything it tries, learns real costs first, and cannot touch its own safety rules.

## What learns, how often, who decides
| Loop | Cadence | What it changes | Automatic? | Human gate |
|---|---|---|---|---|
| Cost learning (`bot.cli costs`) | every `nightly` (increases) | per-class spread/slippage in `config/costs_learned.yaml` | **Increases only** (reality worse than the model) | Decreases need `--apply --by <you>` |
| Research budget (`config/research_budget.yaml`) | continuous | how many NEW variants may be tested per quarter, and the Deflated-Sharpe bar | Enforced in code | Only you can raise the budget (log why in DECISIONS.md) |
| Learning cycle (`bot.cli learn`) | monthly | registers screened variants as **shadow** challengers | Runs the screen | None needed: it cannot change the live book |
| Shadow test (`bot.cli shadow`) | weekly | measures a challenger on sessions AFTER its data-through date | Yes | - |
| Promotion (`bot.cli promote ID --approve you`) | rare | swaps one strategy's parameters in `config/selected.yaml` | **No** | Required, and refused unless the shadow test passed |
| Drift to cash (`monitor/drift.py`) | daily | scales targets to 0 (`state/CASH_MODE`) | Yes | You lift it |

Never in any loop: risk limits, the evidence gate, kill switch state, live trading, key handling.

## 1. Costs first
The plan lives or dies on small net wins, so the cost model is the most valuable thing to calibrate.
- Every fill is recorded with its reference price (the session open) and fee. Slippage = how much worse than the open
  you were filled (buys: fill/open - 1, sells: open/fill - 1), in bps.
- Local-simulator fills equal the model by construction, so they are **excluded**. Only broker-paper or live fills teach.
- Per asset class: 10% trimmed mean, bootstrap 95% CI, shrunk toward the current model with 30 pseudo-observations,
  bounded to 0.5x-2x of the model per update.
- Increase if >= 10 fills and the shrunk estimate is > 10% above the model. Decrease only if >= 60 fills, the CI is
  entirely below the model and a person signs. Every change is appended to `state/costs_history.jsonl`.
- `reports/costs_calibration.md` also shows the **break-even cost multiplier** (`costs --breakeven`): how many times
  higher than modelled the costs can get before the selected book's net Sharpe reaches zero, and the round-trip cost by
  order size including fixed fees.
- Each run record stores a hash of the effective cost files, so you can see which costs a result used.

## 2. The research budget (alpha-spending, in plain terms)
- The pre-registered grid (`config/strategy.yaml`) is free ("founding").
- Any new (strategy, parameters) combination spends 1 unit. Default: **24 per calendar quarter**. Over budget, the run is
  refused before anything is evaluated, and proposals stay queued.
- The Deflated-Sharpe bar starts at 0.90 and rises by 0.02 for every 8 units used that quarter (cap 0.99).
- The trial count behind the Deflated Sharpe is every combination ever tried, so a bot that has searched for years needs
  a higher Sharpe from each new idea.
- Re-evaluating known combinations on newer data is free, because that is exactly what new data is for.
- `python -m bot.cli budget` shows the current state.

## 3. Cycle: propose -> screen -> shadow -> promote
1. Anyone (you, or an LLM) writes proposals: `{"strategy": "trend", "grid": {"sma": [120]}, "rationale": "..."}` and runs
   `python -m bot.cli propose file.json`. Validation rejects anything that is not a registered strategy's own tunable
   parameter (so nothing about risk, gates, paths or live trading can be queued). Max 3 per call, duplicates dropped.
2. `python -m bot.cli learn` takes up to 3 queued proposals, charges the budget, runs the full walk-forward harness on the
   extended grid with the raised bar, and registers a **shadow challenger** only if the strategy is KEEP and the chosen
   parameters are genuinely new.
3. `python -m bot.cli shadow` simulates the challenger's book next to the champion's on sessions after its
   data-through date. It is promotable only after **126 new sessions** (about six months) with the improvement's 95% CI
   above zero at normal and doubled costs, and no worse than 1.2x the champion's drawdown.
4. `python -m bot.cli promote ID --approve "<you>"` swaps the parameters, backs up the old selection to
   `state/selected_backups/`, and logs the evidence to `state/promotions.jsonl`. Restore by copying the backup back.

New data is the only honest test: the original 2-year holdout is spent once `holdout` has run.

## 4. What this cannot do
- It cannot create an edge that is not there; a quiet year with zero promotions is a normal, healthy outcome.
- Six months of shadow data can only confirm large improvements. Small real improvements will usually not be proven;
  the system is designed to prefer missing them over adopting luck.
- Cost learning from paper fills may flatter reality. Re-calibrate as soon as there are real fills.
- Regime changes can break a validated strategy before any statistic notices. The drift monitor and the drawdown ladder
  limit the damage; they do not predict it.

## Not built on purpose
Live reinforcement learning, gradient-boosted price models in the trading path, self-editing configuration, an LLM that
places or sizes orders, and automatic promotion.
