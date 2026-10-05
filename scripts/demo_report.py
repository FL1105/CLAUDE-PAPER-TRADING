#!/usr/bin/env python3
"""Build a SYNTHETIC demo report (out/demo/) to preview dashboard.html with no API access.

Everything is labeled evidence_mode="synthetic": strict gate 1 fails and the tier is
capped. Usage: python scripts/demo_report.py [--world skilled|null] [--seed N]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from polybot.config import config_hash, load_config
from polybot.data.schemas import MarketRecord, ResolutionStatus, TokenRecord, TradeRecord
from polybot.data.store import Store
from polybot.data.synthetic import DAY, T0, generate_world
from polybot.execution.replay import replay, tape_price_fn
from polybot.report.build import write_outputs
from polybot.selection.walkforward import walk_forward

ROOT = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--world", default="skilled", choices=["skilled", "null"])
ap.add_argument("--seed", type=int, default=7)
args = ap.parse_args()

base = load_config()
cfg = base.model_copy(
    update={
        "synthetic": base.synthetic.model_copy(
            update={"sigma_noise": 0.15, "skilled_scan_multiplier": 8.0}
        ),
        "execution": base.execution.model_copy(update={"sim_half_spread": 0.005}),
    }
)
h = config_hash(cfg)
out = ROOT / "out" / "demo"
out.mkdir(parents=True, exist_ok=True)
db = out / "demo.sqlite"
db.unlink(missing_ok=True)
w = generate_world(cfg.synthetic, args.seed, args.world)  # type: ignore[arg-type]

with Store(db) as st:
    st.upsert_markets(
        (
            MarketRecord(
                condition_id=f"m{r.market}",
                question=f"synthetic market {r.market}",
                end_ts=int(r.end_ts),
                resolved_ts=int(r.end_ts),
                resolution_status=ResolutionStatus.RESOLVED,
                winning_outcome_index=int(r.winning_outcome),
                settlement_prices=(1.0, 0.0) if r.winning_outcome == 0 else (0.0, 1.0),
                category=None,
                neg_risk=False,
            ),
            [
                TokenRecord(
                    token_id=f"m{r.market}:{i}",
                    condition_id=f"m{r.market}",
                    outcome_index=i,
                    outcome_label=lbl,
                )
                for i, lbl in enumerate(["Yes", "No"])
            ],
        )
        for r in w.markets.itertuples()
    )
    st.insert_trades(
        (
            TradeRecord(
                trade_key=f"k{i}",
                tx_hash=f"0x{i:x}",
                ts=int(r.ts),
                wallet=str(r.wallet),
                condition_id=f"m{r.market}",
                token_id=f"m{r.market}:{int(r.outcome)}",
                side="BUY",
                price=float(r.price),
                size_shares=float(r.shares),
                size_usdc=float(r.cost),
                outcome_index=int(r.outcome),
            )
            for i, r in enumerate(w.trades.itertuples())
        ),
        "synthetic",
    )
    trades, markets = st.trades_frame(), st.markets_frame()
    start, end = T0 + 60 * DAY, T0 + 180 * DAY
    wf = walk_forward(trades, markets, cfg, start, end, seed=args.seed)
    for e in wf.epochs:
        st.save_frozen_set(
            {
                "epoch_start": e["start_ts"],
                "epoch_end": e["end_ts"],
                "wallets_json": json.dumps(e["selected"]),
                "eligible_json": json.dumps({k: list(v) for k, v in e["eligible"].items()}),
                "diagnostics_json": json.dumps(e["diagnostics"], default=float),
                "leakage_failures": 0,
                "config_hash": h,
                "created_ts": e["start_ts"],
            }
        )
    st.set_meta("forward_start_ts", str(start))
    st.set_meta("forward_config_hash", h)
    eps = [(e["start_ts"], e["end_ts"], tuple(e["selected"])) for e in wf.epochs]
    replay(
        trades,
        markets,
        eps,
        cfg,
        tape_price_fn(trades),
        evidence_mode="synthetic",
        config_hash=h,
        store=st,
    )
    rep = write_outputs(st, cfg, out, now=end, evidence_mode="synthetic")
print(
    f"SYNTHETIC demo ({args.world}, seed {args.seed}): tier={rep['ladder']['tier']} "
    f"settled={rep['summary']['settled_trades']} roi={rep['summary']['roi_net']}"
)
print(f"Open dashboard.html and load {out}/report.json and {out}/trades.csv")
