"""Full evaluation pipeline: walk-forward -> realistic fills -> stress -> placebo -> gates.

Assumptions:
    * Inputs are the analysis frames (trades: wallet, market, outcome, side, price,
      shares, ts; markets: market, winning_outcome, resolved_ts [, q]).
    * ``price_at`` supplies the delayed fill reference: drift (synthetic) or the
      tape (real history).
    * Placebo: for each epoch, every selected wallet is replaced by a random
      ELIGIBLE, non-selected wallet from the same activity bucket (quintile of
      resolved markets) and entry-price bucket (tercile of mean entry price). That
      is stricter than "matched on trade count" (DECISIONS D25). The identical
      copy engine (``fastsim``, verified equal to ``replay``) scores each draw.
    * Stress = 2x fees AND 2x spread (slippage) AND 2x latency in one run.

Failure modes guarded against: every number the gates need comes from one
place, with one seed, so reports are reproducible and nothing is cherry-picked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from polybot.config import Config
from polybot.execution.fastsim import fast_copy, prepare_signals
from polybot.execution.replay import PriceAt, ReplayResult, replay
from polybot.gates import metrics as M  # noqa: N812
from polybot.gates.gates import Admissibility, GateResult, all_pass, evaluate
from polybot.selection.walkforward import WalkForwardResult, walk_forward


@dataclass
class BacktestResult:
    evidence: M.Evidence
    gates: list[GateResult]
    walkforward: WalkForwardResult
    replay: ReplayResult
    placebo_computed: bool
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def promoted(self) -> bool:
        return all_pass(self.gates)


def _buckets(values: dict[str, tuple[int, float]]) -> dict[str, tuple[int, int]]:
    if not values:
        return {}
    w = list(values)
    n = np.array([values[k][0] for k in w], float)
    p = np.array([values[k][1] for k in w], float)
    qn = np.searchsorted(np.quantile(n, [0.2, 0.4, 0.6, 0.8]), n, side="right")
    qp = np.searchsorted(np.quantile(p, [1 / 3, 2 / 3]), p, side="right")
    return {k: (int(a), int(b)) for k, a, b in zip(w, qn, qp, strict=True)}


def placebo_sets(
    epochs: list[dict[str, Any]], n_draws: int, seed: int
) -> list[list[tuple[int, int, tuple[str, ...]]]]:
    """``n_draws`` placebo frozen-set schedules, matched per epoch and per selected wallet."""
    rng = np.random.default_rng(seed)
    prepared = []
    for e in epochs:
        elig: dict[str, tuple[int, float]] = e["eligible"]
        sel = list(e["selected"])
        b = _buckets(elig)
        pool_by: dict[tuple[int, int], list[str]] = {}
        act_by: dict[int, list[str]] = {}
        for wlt, key in sorted(b.items()):
            if wlt in sel:
                continue
            pool_by.setdefault(key, []).append(wlt)
            act_by.setdefault(key[0], []).append(wlt)
        prepared.append((e["start_ts"], e["end_ts"], sel, b, pool_by, act_by))
    draws = []
    for _ in range(n_draws):
        sched = []
        for start, end, sel, b, pool_by, act_by in prepared:
            chosen: list[str] = []
            for wlt in sel:
                bk = b.get(wlt)
                pool = [x for x in pool_by.get(bk, []) if x not in chosen] if bk else []
                if not pool and bk:  # fall back to activity bucket only
                    pool = [x for x in act_by.get(bk[0], []) if x not in chosen]
                if not pool:
                    pool = [x for v in act_by.values() for x in v if x not in chosen]
                if pool:
                    chosen.append(pool[int(rng.integers(len(pool)))])
            sched.append((start, end, tuple(sorted(chosen))))
        draws.append(sched)
    return draws


def settled_trades(rep: ReplayResult, epochs: list[dict[str, Any]]) -> pd.DataFrame:
    if rep.fills.empty or rep.settlements.empty:
        return pd.DataFrame(columns=list(M.TRADE_COLUMNS))
    m = rep.fills.merge(rep.settlements[["fill_id", "net_pnl", "resolved_ts"]], on="fill_id")
    starts = np.array([e["start_ts"] for e in epochs])
    ep = np.searchsorted(starts, m["created_ts"].to_numpy(), side="right") - 1
    return pd.DataFrame(
        {
            "market": m["condition_id"].astype(str),
            "wallet": m["wallet"].astype(str),
            "stake": m["stake_usdc"],
            "fee": m["fee"],
            "net_pnl": m["net_pnl"],
            "resolved_ts": m["resolved_ts"],
            "epoch": ep,
            "slippage": m["slippage"],
        }
    )


def run_backtest(
    trades: pd.DataFrame,
    markets: pd.DataFrame,
    cfg: Config,
    *,
    start_ts: int,
    end_ts: int,
    price_at: PriceAt,
    mode: str,
    seed: int,
    config_hash: str,
    admissibility: Admissibility = "strict",
    lazy_placebo: bool = False,
    leakage_failures: int = 0,
    recent_drift_errors: int = 0,
) -> BacktestResult:
    """``lazy_placebo``: skip the (expensive) placebo when another gate already fails.
    Used by large simulation runs; promotion decisions are identical either way."""
    wf = walk_forward(trades, markets, cfg, start_ts, end_ts, seed=seed)
    eps = [(e["start_ts"], e["end_ts"], tuple(e["selected"])) for e in wf.epochs]
    rep = replay(
        trades,
        markets,
        eps,
        cfg,
        price_at,
        evidence_mode="synthetic" if mode == "synthetic" else "backtest",
        config_hash=config_hash,
    )
    st = settled_trades(rep, wf.epochs)
    rows = prepare_signals(trades, eps, cfg)
    stress = fast_copy(
        rows,
        markets,
        cfg,
        price_at,
        fee_multiplier=cfg.fees.stress_multiplier,
        spread_multiplier=2.0,
        latency_multiplier=2.0,
    )
    s = rep.summary
    agg = wf.aggregate["selected"]
    lat = {int(k[:-2]): v for k, v in s.get("roi_by_latency", {}).items()}
    reached = s["signals"] - s.get("capacity_skipped", 0)

    def make(placebo: tuple[float, ...] | None) -> M.Evidence:
        return M.Evidence(
            mode=mode,
            start_ts=start_ts,
            end_ts=end_ts,
            trades=st,
            bankroll=cfg.paper.bankroll_usdc,
            signals_reached_book=reached,
            signals_filled=s["filled"],
            median_slippage=s.get("median_slippage"),
            pooled_oos_z=agg.get("pooled_z"),
            frac_wallets_positive_oos=agg.get("frac_wallets_positive_oos"),
            epoch_pooled_z=tuple(e["oos_pooled_z"] for e in wf.epochs),
            stress_roi=stress.roi,
            latency_roi=lat,
            placebo_rois=placebo,
            leakage_failures=leakage_failures,
            recent_drift_errors=recent_drift_errors,
            config_hash_start=config_hash,
            config_hash_now=config_hash,
        )

    ev = make(None)
    gates = evaluate(ev, cfg, admissibility)
    others_pass = all(g.passed for g in gates if not g.name.startswith("4_"))
    computed = False
    if not lazy_placebo or others_pass:
        draws = placebo_sets(wf.epochs, cfg.placebo.n_draws, seed + 7919)
        rois = []
        for sched in draws:
            r = fast_copy(prepare_signals(trades, sched, cfg), markets, cfg, price_at).roi
            rois.append(r if r is not None else 0.0)
        ev = make(tuple(rois))
        gates = evaluate(ev, cfg, admissibility)
        computed = True
    extras = {
        "replay_summary": s,
        "stress": stress.__dict__ | {"roi": stress.roi},
        "walkforward_aggregate": wf.aggregate,
    }
    return BacktestResult(ev, gates, wf, rep, computed, extras)
