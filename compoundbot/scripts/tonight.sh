#!/usr/bin/env bash
# One command for the first real-data session. Run from the compoundbot folder:  bash scripts/tonight.sh
# Needs Python 3.11+. Put PAPER keys in .env first (copy .env.example). Never paste keys anywhere else.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-python3}
$PY -m venv .venv 2>/dev/null; . .venv/bin/activate 2>/dev/null || . .venv/Scripts/activate
pip install -q -e ".[dev,dash]" || { echo "install failed"; exit 1; }
echo "== tests (about a minute)"; python -m pytest -q -m "not slow" || echo "TESTS FAILED: continue only if you understand why"
echo "== Alpaca probe (read-only)"; python -m bot.cli probe || echo "probe had failures (details in reports/alpaca_probe.json)"
SRC=${SRC:-yahoo}
echo "== data from $SRC (set SRC=csv or SRC=alpaca to change)"
python -m bot.cli data --source "$SRC" --start 2003-01-01 || { echo "data step failed"; python -m bot.cli summary; exit 1; }
echo "== research (10-30 min)"; python -m bot.cli research || true
python -m bot.cli select || true     # writes selected.yaml WITHOUT approval: the bot will not trade yet
python -m bot.cli backtest || true
python -m bot.cli summary
echo; echo "Paste reports/TONIGHT_SUMMARY.md back to Claude. It contains no keys or account ids."
