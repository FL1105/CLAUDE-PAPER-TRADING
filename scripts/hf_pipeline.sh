#!/usr/bin/env bash
# One full real-data walk-forward run on a window of the Hugging Face dataset.
# Usage: scripts/hf_pipeline.sh NAME START END SAMPLE [MAX_TRADES]
#   e.g. scripts/hf_pipeline.sh early 2026-01-06 2026-04-07 10000
# Everything lands in out/<NAME>/ (own SQLite DB, shortlist, mapping, config, logs).
set -euo pipefail
NAME=$1; START=$2; END=$3; SAMPLE=$4; MAXT=${5:-1000}
ROOT=$(cd "$(dirname "$0")/.." && pwd); cd "$ROOT"
D=out/$NAME; mkdir -p "$D"
python scripts/hf_shortlist.py --start "$START" --end "$END" --sample "$SAMPLE" \
  --max-trades "$MAXT" --out "$D/wallets.parquet" | tee "$D/shortlist.log"
sed "s#out/hf/wallets.parquet#$D/wallets.parquet#" config/history/hf_sii_taker.sql > "$D/mapping.sql"
python - "$NAME" <<'PY'
import sys, yaml
n = sys.argv[1]
d = yaml.safe_load(open("config/backtest_90d.yaml"))
d["storage"]["db_path"] = f"out/{n}/polybot.sqlite"
open(f"out/{n}/config.yaml", "w").write(yaml.safe_dump(d, sort_keys=False))
PY
polybot history import "hf://datasets/SII-WANGZJ/Polymarket_data/trades.parquet" \
  --name "hf-$NAME" --start "$START" --end "$END" --mapping "$D/mapping.sql" -c "$D/config.yaml" \
  > "$D/import.log" 2>&1
polybot history markets -c "$D/config.yaml" | tee "$D/markets.log"
polybot backtest --days 48 -c "$D/config.yaml" > "$D/backtest.log" 2>&1 || true
grep -v '"ts":' "$D/backtest.log" | tail -n 16
