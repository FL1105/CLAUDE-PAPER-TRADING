#!/usr/bin/env bash
# Unattended research queue (paper / read-only only). Results land in out/overnight/ and are
# summarised into docs by hand afterwards. Safe to re-run: finished steps are skipped.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd); cd "$ROOT"
O=out/overnight; mkdir -p "$O" out/maker
step() { # name, command...
  local n=$1; shift
  [ -e "$O/$n.done" ] && return 0
  echo "$(date -u +%FT%TZ) start $n" >> "$O/queue.log"
  if "$@" > "$O/$n.out" 2>&1; then touch "$O/$n.done"; fi
  echo "$(date -u +%FT%TZ) end $n" >> "$O/queue.log"
}
wait_for() { while pgrep -f "$1" >/dev/null; do sleep 30; done; }

wait_for "maker record"
step maker_grid_long python scripts/maker_grid.py out/maker/long.sqlite
step maker_rec_2 polybot maker record --minutes 120 --markets 20 --interval 6 --db out/maker/rec2.sqlite
step maker_grid_2 python scripts/maker_grid.py out/maker/rec2.sqlite
for d in 2026-03-10 2026-05-12 2026-07-14 2026-09-15; do
  step "markout_$d" python scripts/markout_study.py --start "$d" --end "$(date -u -d "$d +2 day" +%F)"
done
for w in "2026-09-01 2026-09-08" "2026-07-01 2026-07-08" "2026-04-01 2026-04-08"; do
  set -- $w; step "btc_$1" python scripts/btc_updown_study.py --start "$1" --end "$2"
done
step maker_rec_3 polybot maker record --minutes 120 --markets 20 --interval 6 --db out/maker/rec3.sqlite
step maker_grid_3 python scripts/maker_grid.py out/maker/rec3.sqlite
wait_for "hf_pipeline"
step late_window scripts/hf_pipeline.sh late 2025-10-06 2026-01-07 10000
echo "$(date -u +%FT%TZ) ALL DONE" >> "$O/queue.log"
