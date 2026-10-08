#!/usr/bin/env bash
# One-command paper "learning" bot (Mac/Linux). Paper only: it never places real orders.
# Usage: ./start_bot.sh        (first run sets up a virtual environment; later runs resume)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/polybot ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -e ".[dev]"
fi
echo "Starting the learning bot. Leave this window open. Press Ctrl+C to stop."
.venv/bin/polybot learn run --minutes 10080
