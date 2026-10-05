# Phase 6 checkpoint: forward paper loop, report, dashboard

## Built
| piece | what it does |
|---|---|
| `polybot paper` (`forward.py`) | Every 10 s: ensures this epoch's **frozen** wallet set exists (selected from data before the epoch start, leakage self-checked, stored append-only), polls the public trade feed, and for each BUY by a frozen wallet fetches the **real book at detection**, then shadow books at +2/+4/+10 s, then writes the fill. Wallet sells are logged, not mirrored; stale detections (> 300 s) are skipped. Settles resolved/void markets every 10 min, evaluates kill switches every 5 min (a trigger halts new entries), heartbeat every 60 s. |
| crash safety | Trade cursor and pending-shadow queue persisted in SQLite. After a restart, state is rebuilt from the append-only ledger. Shadows that can't be captured honestly after a crash are recorded as `missing`. On first start the cursor is "now", so history is never copied. |
| `polybot settle` | the settlement step on demand |
| `polybot report` (`report/build.py`) | live evidence from SETTLED ledger rows only -> all 12 gates (strict), tier and blocking gates, power/MDE statement, per-epoch table, selection diagnostics, stress ROI, latency curve from real shadow books, same-engine placebo, concentration, skip reasons, kill switches, equity series, manual checklist. Writes `out/report.json`, `out/trades.csv`, `out/equity.csv`. |
| `dashboard.html` | one offline file (no network, no CDN, no libraries); dark by default with a light toggle; phone-friendly. Loads report.json + trades.csv by picker or drag-drop and remembers them (localStorage, guarded). Shows the tier ladder, gate checklist with plain-language reasons, equity, drawdown, edge-vs-latency and placebo charts with hover, a trades table with search, filters, sort and pagination, downloads, and the manual checklist. |
| `scripts/demo_report.py` | builds a clearly-labeled SYNTHETIC report through the real pipeline (DB -> frozen sets -> ledger -> report), to preview the dashboard without API access |
| RUNBOOK | tmux, macOS launchd and Linux systemd recipes; health checks; kill-switch review; backups |

## Verified
- End-to-end forward test against a fake API: stale skip, non-frozen wallet ignored,
  fill decided on the detection-time book (0.51) while shadows saw 0.52/0.53/0.54,
  exit logged, settlement on resolution, report written, restart without
  reprocessing. Crash with a pending signal recovers. A halt blocks new entries.
- Dashboard rendered in headless Chromium at 390 px and 1200 px: no JS errors,
  **zero network requests**, no horizontal overflow on the phone after a fix, hover
  tooltips work, the theme toggle works, data survives a reload. Chart colors pass
  the colorblind-safety validator in both themes.
- 205 tests pass (1 skipped until live fixtures exist); coverage 94% overall.

## Fixed during this phase
- The tier ladder overflowed a 390 px phone screen; it now wraps to two columns.
- Edge tick labels on charts were clipped; they are now anchored at the ends.
- A tiny p-value was shown as "0.0e+0"; it now shows "< 1e-15".

## Not verifiable here
- Nothing has run against the real API (network blocked in the build sandbox).
  The forward loop is only as good as the probed schemas: run `polybot probe` first.
