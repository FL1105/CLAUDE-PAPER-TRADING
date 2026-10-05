# RUNBOOK

## 1. Setup on your computer (one time)

What you need to install:

- **Python 3.11 or newer**: <https://www.python.org/downloads/>. On macOS,
  `brew install python@3.12` also works. Check with `python3 --version`.
- **git**: <https://git-scm.com/downloads> (preinstalled on macOS/Linux).
- Python packages are installed by `pip` in the steps below. There are no API
  keys, no wallet and no `.env` file. Nothing else needs to be downloaded.

```bash
git clone https://github.com/FL1105/CLAUDE-PAPER-TRADING.git
cd CLAUDE-PAPER-TRADING
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                             # should be all green
```

## 2. Verify the API (do this first)

```bash
polybot probe            # or: python scripts/probe_apis.py
pytest                   # now also validates the adapters against real responses
```

This writes `tests/fixtures/live/*.json`, `out/probe_report.json` and the
auto-generated findings section of `docs/ASSUMPTIONS.md`. Exit codes:
`0` ok, `1` nothing reachable (VPN / region / firewall), `3` schema drift
(fix `src/polybot/data/schemas.py` before going further).

Polymarket may restrict access from some regions. The probe only reads public
data, but if every endpoint fails, check whether your network can reach
`gamma-api.polymarket.com` in a browser.

## 3. Build the historical database (Phase 3)

```bash
polybot backfill --days 90           # API: trades feed + markets (resumable; re-run is safe)
#   if it reports the offset cap, add history from a bulk dataset:
pip install -e ".[history]"
polybot history inspect PATH         # see docs/HISTORY_IMPORT.md
polybot history import PATH --name SRC --start 2026-04-01 --end 2026-10-01
polybot backfill --markets-only      # attach markets / resolutions to imported trades

polybot select --epoch-start 2026-09-01   # one epoch: selected wallets + filter counts
polybot walkforward --days 90             # OOS by epoch (gross, before costs)
```

The database lives at `out/polybot.sqlite`. Delete it to start over.

## 4. Forward paper run (Phase 6, not built yet)

Will be documented here with `systemd` / `launchd` / `tmux` recipes once
`polybot paper` exists.
