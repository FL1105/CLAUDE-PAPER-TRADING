"""Live-forward evidence and the report: out/report.json, out/trades.csv, out/equity.csv.

Assumptions:
    * Evidence for gates comes ONLY from ledger rows with evidence_mode
      "live_forward" and only from SETTLED trades (mark-to-market is shown in the
      report but never used by a gate).
    * Persistence (gate 5) is measured on the frozen sets' own trades during each
      forward epoch, at the wallets' prices, settled by final outcomes.
    * Gate 4 (placebo) scores the strategy AND 200 placebo draws with the same
      engine on the same forward-period tape, so both sides are comparable.
    * Stress (gate 6) re-prices each settled fill with its 2xL shadow fill, the
      slippage-vs-best counted twice, and fees x ``stress_multiplier``.

Failure mode guarded against: a report that looks better than the ledger. Every
number is derived from the append-only ledger plus the config hash it was
produced under.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from polybot.backtest import placebo_sets
from polybot.config import Config, config_hash
from polybot.data.store import Store
from polybot.execution.fastsim import fast_copy, prepare_signals
from polybot.execution.replay import tape_price_fn
from polybot.gates import metrics as M  # noqa: N812
from polybot.gates.ladder import kill_switches, ladder
from polybot.selection.walkforward import oos_stats
from polybot.stats.bootstrap import power_statement
from polybot.stats.edge import build_units

DAY = 86_400
EMPTY_TRADES = pd.DataFrame(columns=[*M.TRADE_COLUMNS, "fill_id", "shares", "payout"])


def _frozen(store: Store) -> list[dict[str, Any]]:
    out = []
    for r in store.frozen_sets().to_dict("records"):
        diag = json.loads(r["diagnostics_json"])
        out.append(
            {
                "start_ts": int(r["epoch_start"]),
                "end_ts": int(r["epoch_end"]),
                "selected": json.loads(r["wallets_json"]),
                "eligible": {k: tuple(v) for k, v in json.loads(r["eligible_json"]).items()},
                "diagnostics": diag,
                "cap": float(diag.get("cap", math.inf)),
                "leakage_failures": int(r["leakage_failures"]),
            }
        )
    return out


def _settled(store: Store, epochs: list[dict[str, Any]], mode: str) -> pd.DataFrame:
    f, s = store.fills_frame(), store.settlements_frame()
    f = f.loc[f["evidence_mode"] == mode] if len(f) else f
    if f.empty or s.empty:
        return EMPTY_TRADES.copy()
    m = f.merge(s[["fill_id", "payout", "net_pnl", "resolved_ts"]], on="fill_id")
    starts = np.array([e["start_ts"] for e in epochs]) if epochs else np.array([0])
    ep = np.searchsorted(starts, m["created_ts"].to_numpy(), side="right") - 1
    return pd.DataFrame(
        {
            "fill_id": m["fill_id"],
            "market": m["condition_id"].astype(str),
            "wallet": m["wallet"].astype(str),
            "stake": m["stake_usdc"],
            "fee": m["fee"],
            "net_pnl": m["net_pnl"],
            "resolved_ts": m["resolved_ts"].fillna(m["created_ts"]),
            "epoch": np.maximum(ep, 0),
            "shares": m["shares"],
            "payout": m["payout"],
            "shadow_fills_json": m["shadow_fills_json"],
            "slippage": m["slippage"],
            "created_ts": m["created_ts"],
        }
    )


def _shadow_curves(
    st: pd.DataFrame, fee_mult: float
) -> tuple[dict[int, float | None], float | None]:
    lat: dict[int, list[float]] = {}
    stress_pnl = stress_cost = 0.0
    for r in st.to_dict("records"):
        pps = float(r["payout"]) / float(r["shares"]) if r["shares"] else 0.0
        sh = json.loads(r["shadow_fills_json"])
        for k, v in sh.items():
            if v.get("status") in ("filled", "partial") and v.get("shares"):
                acc = lat.setdefault(int(k), [0.0, 0.0])
                acc[0] += float(v["shares"]) * pps - float(v["cost"]) - float(v["fee"])
                acc[1] += float(v["cost"])
        v2 = sh.get("2", {})
        if v2.get("status") in ("filled", "partial") and v2.get("shares"):
            extra = float(v2["shares"]) * max(
                0.0, float(v2["avg_price"]) - float(v2.get("best_ask") or v2["avg_price"])
            )
            stress_pnl += (
                float(v2["shares"]) * pps - float(v2["cost"]) - extra - fee_mult * float(v2["fee"])
            )
            stress_cost += float(v2["cost"])
    curve = {k: (a / c if c > 0 else None) for k, (a, c) in sorted(lat.items())}
    return curve, (stress_pnl / stress_cost if stress_cost > 0 else None)


def live_evidence(
    store: Store,
    cfg: Config,
    now: int,
    *,
    with_placebo: bool = True,
    evidence_mode: str = "live_forward",
) -> tuple[M.Evidence, dict[str, Any]]:
    fs_raw = store.get_meta("forward_start_ts")
    start = int(fs_raw) if fs_raw else now
    epochs = _frozen(store)
    st = _settled(store, epochs, evidence_mode)
    sigs = store.signals_frame()
    entries = sigs.loc[sigs["status"].isin(["filled", "skipped"])] if len(sigs) else sigs
    if len(entries):
        own = entries["skip_reason"].fillna("").str.match(r"^(risk:|halted:)")
        reached = int((~own).sum())
        filled = int((entries["status"] == "filled").sum())
    else:
        reached = filled = 0
    fills = store.fills_frame()
    live_fills = fills.loc[fills["evidence_mode"] == evidence_mode] if len(fills) else fills
    med_slip = (
        float(live_fills["slippage"].dropna().median())
        if len(live_fills) and live_fills["slippage"].notna().any()
        else None
    )

    trades, markets = store.trades_frame(), store.markets_frame()
    final = markets[["market", "winning_outcome"]]
    per_ep, num, den, wz_all = [], 0.0, 0.0, []
    for e in epochs:
        inside = trades.loc[
            (trades["ts"] >= e["start_ts"]) & (trades["ts"] < min(e["end_ts"], now))
        ]
        s = oos_stats(build_units(inside, final).units, tuple(e["selected"]), e["cap"])
        per_ep.append(s["pooled_z"])
        num += s["num"]
        den += s["den"]
        wz_all.extend(s["wallet_z"].values())
    lat, stress = _shadow_curves(st, cfg.fees.stress_multiplier) if len(st) else ({}, None)

    placebo: tuple[float, ...] | None = None
    strat_engine: float | None = None
    eps_sched = [(e["start_ts"], min(e["end_ts"], now), tuple(e["selected"])) for e in epochs]
    if with_placebo and any(e["selected"] for e in epochs) and len(trades):
        pf = tape_price_fn(trades)
        strat_engine = fast_copy(prepare_signals(trades, eps_sched, cfg), markets, cfg, pf).roi
        draws = placebo_sets(
            [{**e, "end_ts": min(e["end_ts"], now)} for e in epochs],
            cfg.placebo.n_draws,
            cfg.seed + 7919,
        )
        placebo = tuple(
            (fast_copy(prepare_signals(trades, d, cfg), markets, cfg, pf).roi or 0.0) for d in draws
        )

    ev = M.Evidence(
        mode=evidence_mode,
        start_ts=start,
        end_ts=now,
        trades=st,
        bankroll=cfg.paper.bankroll_usdc,
        signals_reached_book=reached,
        signals_filled=filled,
        median_slippage=med_slip,
        pooled_oos_z=(num / math.sqrt(den)) if den > 0 else None,
        frac_wallets_positive_oos=float(np.mean([z > 0 for z in wz_all])) if wz_all else None,
        epoch_pooled_z=tuple(per_ep),
        stress_roi=stress,
        latency_roi=lat,
        placebo_rois=placebo,
        placebo_strategy_roi=strat_engine,
        leakage_failures=sum(e["leakage_failures"] for e in epochs),
        recent_drift_errors=store.drift_count_since(
            now - cfg.gates.schema_drift_lookback_days * DAY
        ),
        config_hash_start=store.get_meta("forward_config_hash") or "",
        config_hash_now=config_hash(cfg),
    )
    return ev, {"epochs": epochs, "signals": sigs, "fills": fills}


def equity_series(st: pd.DataFrame, bankroll: float) -> list[dict[str, float]]:
    if st.empty:
        return []
    o = M.ordered(st)
    eq = bankroll + np.cumsum(o["net_pnl"].to_numpy(dtype=np.float64))
    peak = np.maximum.accumulate(np.r_[bankroll, eq])[1:]
    return [
        {
            "ts": int(t),
            "equity": round(float(e), 4),
            "drawdown": round(float((p - e) / bankroll), 6),
        }
        for t, e, p in zip(o["resolved_ts"], eq, peak, strict=True)
    ]


def build_report(
    store: Store,
    cfg: Config,
    now: int | None = None,
    backtest_path: Path | None = None,
    evidence_mode: str = "live_forward",
) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    ev, ctx = live_evidence(store, cfg, now, evidence_mode=evidence_mode)
    lad = ladder(ev, cfg, "strict")
    st = ev.trades
    sigs: pd.DataFrame = ctx["signals"]
    skip = (
        sigs.loc[sigs["status"] == "skipped", "skip_reason"].value_counts()
        if len(sigs)
        else pd.Series(dtype=int)
    )
    mk = st.groupby("market")["net_pnl"].sum() if len(st) else pd.Series(dtype=float)
    ret = (
        (st.groupby("market")["net_pnl"].sum() / st.groupby("market")["stake"].sum())
        if len(st)
        else pd.Series(dtype=float)
    )
    sigma = float(ret.std(ddof=1)) if len(ret) > 1 else float("nan")
    roi = M.roi(st)
    epochs = ctx["epochs"]
    ep_rows = {e["epoch"]: e for e in M.epoch_table(st, ev.bankroll)}
    epoch_table = []
    for i, e in enumerate(epochs):
        row = ep_rows.get(i, {})
        epoch_table.append(
            {
                "epoch": i,
                "start_ts": e["start_ts"],
                "end_ts": e["end_ts"],
                "n_selected": len(e["selected"]),
                "settled_trades": row.get("n", 0),
                "pnl": row.get("pnl", 0.0),
                "roi": row.get("roi"),
                "oos_pooled_z": ev.epoch_pooled_z[i] if i < len(ev.epoch_pooled_z) else None,
                "leakage_failures": e["leakage_failures"],
            }
        )
    latest = epochs[-1]["diagnostics"] if epochs else {}
    placebo = list(ev.placebo_rois or ())
    strat = ev.placebo_strategy_roi
    report: dict[str, Any] = {
        "generated_ts": now,
        "config_hash": config_hash(cfg),
        "forward_config_hash": ev.config_hash_start,
        "evidence_mode": evidence_mode,
        "date_range": {
            "start_ts": ev.start_ts,
            "end_ts": ev.end_ts,
            "days": round((ev.end_ts - ev.start_ts) / DAY, 2),
        },
        "halted": store.get_halt(),
        "summary": {
            "settled_trades": len(st),
            "markets": int(st["market"].nunique()) if len(st) else 0,
            "roi_net": roi,
            "pnl": float(st["net_pnl"].sum()) if len(st) else 0.0,
            "max_drawdown": M.max_drawdown(st, ev.bankroll),
            "open_positions": int(len(store.fills_frame()) - len(store.settlements_frame())),
            "fill_rate": (ev.signals_filled / ev.signals_reached_book)
            if ev.signals_reached_book
            else None,
            "median_slippage": ev.median_slippage,
        },
        "power": {
            "sigma_per_market": None if math.isnan(sigma) else sigma,
            "statement": power_statement(sigma, len(ret), 0.05, cfg.stats.alpha, cfg.stats.power)
            if len(ret) > 1 and not math.isnan(sigma)
            else "Not enough settled markets yet to estimate power.",
        },
        "ladder": lad.as_dict(),
        "epochs": epoch_table,
        "selection": {
            "latest_epoch_diagnostics": latest,
            "naive_vs_bh_note": "naive_top_pnl is the contrast list a 'top traders by PnL' "
            "approach would copy; it is never traded.",
        },
        "stress": {"roi_2x_fee_2x_slippage_2x_latency": ev.stress_roi},
        "latency_curve": {f"{k}xL": v for k, v in sorted(ev.latency_roi.items())},
        "placebo": {
            "n": len(placebo),
            "rois": placebo,
            "strategy_roi_same_engine": strat,
            "strategy_percentile": float(np.mean(np.asarray(placebo) < strat) * 100)
            if placebo and strat is not None
            else None,
        },
        "concentration": {
            "top_markets": [
                {"market": k, "pnl": float(v)}
                for k, v in mk.sort_values(ascending=False).head(10).items()
            ],
            "top_market_share": M.concentration(st, "market") if len(st) else None,
            "top_wallet_share": M.concentration(st, "wallet") if len(st) else None,
        },
        "skip_reasons": {str(k): int(v) for k, v in skip.items()},
        "kill_switches": [k.__dict__ for k in kill_switches(ev, cfg)],
        "equity": equity_series(st, ev.bankroll),
        "anomalies": store.anomaly_counts(),
        "assumptions": [
            "API schemas and fee formula: see docs/ASSUMPTIONS.md (verify with `polybot probe`).",
            "Gates use settled PnL only; open positions are not marked to market here.",
            "Placebo and gate 4 use the same engine for strategy and placebo draws.",
        ],
    }
    if backtest_path is not None and backtest_path.exists():
        bt = json.loads(backtest_path.read_text())
        report["backtest"] = {
            "tier": bt["ladder"]["tier"],
            "replay": bt.get("replay_summary"),
            "note": bt.get("note"),
        }
    return report


def trades_csv(store: Store) -> pd.DataFrame:
    s, f, st = store.signals_frame(), store.fills_frame(), store.settlements_frame()
    if s.empty:
        return pd.DataFrame(
            columns=[
                "signal_id",
                "created_ts",
                "wallet",
                "condition_id",
                "token_id",
                "status",
                "skip_reason",
            ]
        )
    cols_f = [
        "signal_id",
        "fill_id",
        "fill_price",
        "best_ask",
        "slippage",
        "fee",
        "stake_usdc",
        "shares",
    ]
    out = s.drop(columns=["config_hash"]).merge(
        f[cols_f] if len(f) else pd.DataFrame(columns=cols_f), on="signal_id", how="left"
    )
    cols_s = ["fill_id", "resolution_status", "resolved_ts", "payout", "net_pnl"]
    out = out.merge(
        st[cols_s] if len(st) else pd.DataFrame(columns=cols_s), on="fill_id", how="left"
    )
    return out


def write_outputs(
    store: Store,
    cfg: Config,
    out_dir: Path,
    now: int | None = None,
    backtest_path: Path | None = None,
    evidence_mode: str = "live_forward",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rep = build_report(store, cfg, now, backtest_path, evidence_mode)
    (out_dir / "report.json").write_text(json.dumps(rep, indent=1, default=str, sort_keys=True))
    trades_csv(store).to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame(rep["equity"], columns=["ts", "equity", "drawdown"]).to_csv(
        out_dir / "equity.csv", index=False
    )
    return rep
