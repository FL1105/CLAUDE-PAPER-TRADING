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

## 4. Forward paper run (the only admissible evidence)

```bash
polybot paper                     # runs until Ctrl-C; safe to stop and restart any time
polybot paper --once              # a single iteration (smoke test)
```

What it does every `paper.poll_interval_s` (10 s): poll the public trade feed. For
each BUY by this epoch's frozen wallets, it fetches the real order book at
detection, then shadow books at +2, +4 and +10 s, and writes the paper fill. It
settles resolved markets every 10 min and checks kill switches every 5 min.
The heartbeat goes to the log and `out/heartbeat.json`.

The frozen wallet set for each 14-day epoch is chosen at the epoch start from
data before it. It cannot be changed afterwards (database trigger).

**Do not edit `config/default.yaml` during a forward run.** Gate 12 compares
the config hash with the one stamped at the start. To change anything, stop, copy
the database away, start a new database (`storage.db_path`) and a new run.

### Keep it running

**tmux (simplest, any OS):**
```bash
tmux new -s polybot
source .venv/bin/activate && polybot paper 2>&1 | tee -a out/paper.log
# detach: Ctrl-b then d      reattach: tmux attach -t polybot
```

**macOS launchd** (`~/Library/LaunchAgents/com.polybot.paper.plist`, then
`launchctl load ~/Library/LaunchAgents/com.polybot.paper.plist`):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.polybot.paper</string>
  <key>WorkingDirectory</key><string>/Users/YOU/CLAUDE-PAPER-TRADING</string>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/CLAUDE-PAPER-TRADING/.venv/bin/polybot</string><string>paper</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/CLAUDE-PAPER-TRADING/out/paper.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/CLAUDE-PAPER-TRADING/out/paper.log</string>
</dict></plist>
```
Keep the Mac awake: `caffeinate -s` in another terminal, or Energy settings.

**Linux systemd** (`~/.config/systemd/user/polybot.service`, then
`systemctl --user enable --now polybot`; logs: `journalctl --user -u polybot -f`):
```ini
[Unit]
Description=polybot forward paper loop
After=network-online.target

[Service]
WorkingDirectory=%h/CLAUDE-PAPER-TRADING
ExecStart=%h/CLAUDE-PAPER-TRADING/.venv/bin/polybot paper
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

### Check on it

```bash
cat out/heartbeat.json            # ts should be < 2 minutes old
polybot report                    # writes out/report.json, trades.csv, equity.csv
polybot gates --from out/report.json --explain
```
Open `dashboard.html` in any browser (phone too: copy the HTML plus the two
output files over) and load `out/report.json` and `out/trades.csv`.
To preview with synthetic data: `python scripts/demo_report.py`, then load
`out/demo/report.json` and `out/demo/trades.csv`.

### Kill switches

When a kill switch fires, the loop **keeps running but opens no new positions**
(it still settles and logs). The report shows a red banner. Review why
(`polybot report`, dashboard), then clear it with
`polybot resume --note "what I checked and why it is OK"`. The note is logged.

### Backups

`out/polybot.sqlite` is the ledger. Back it up while the loop is running with
`sqlite3 out/polybot.sqlite ".backup out/backup.sqlite"`.

## 5. What "done" looks like

After at least 30 days and 300 settled trades, `polybot report` shows the tier.
Most likely outcomes, in order: **no wallets selected** (thin evidence), **gates
failing** (no copyable edge after costs), or rarely **Rare (S2)**. Every one of
these is a valid answer. Rare means you may *consider* micro-live after the manual
checklist. Nothing in this repo can trade real money.
