# CompoundBot project rules

Paper-first, lower-volatility multi-asset system aiming at small NET wins after fees.
Full build prompt: docs/MASTER_PROMPT.md (the source of truth for rules and gates).

## Rules (short form)
1. No profitability claim without net-of-cost, out-of-sample numbers and a confidence interval.
2. No lookahead; signals use data up to the decision time, trades happen after it.
3. Never invent API fields, fees or data; verify in official docs and log in docs/SOURCES.md.
4. Hard risk limits live in code/config (config/risk.yaml, hash-checked). No model or job edits them.
5. Default mode is PAPER. No real-money order code exists. tests/unit/test_safety.py enforces it.
6. Secrets only in .env (gitignored).
7. Every strategy variant tried is appended to experiments.jsonl.
8. Label results MEASURED / SYNTHETIC / ESTIMATED. SYNTHETIC numbers prove code works, not that markets pay.
9. Daily bars only for the core.
10. Reports show gross AND net with cost assumptions on top.

## How to work
Build step by step, test, commit, log in BUILD_LOG.md. Ambiguity -> docs/ASSUMPTIONS.md.
Stop only at the human gates (docs/MASTER_PROMPT.md, human_gates).
Run: `make setup && make test`. Python from a venv; package is `bot` under src/.
