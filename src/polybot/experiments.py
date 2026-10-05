"""Synthetic-world experiments (Phase 2 checkpoint; extended with gates in Phase 5).

1. ``null_calibration``: over many seeds of the null world, are per-wallet
   p-values Uniform(0, 1)? Does BH stay quiet? Is the small-n exact p-value
   needed?
2. ``selection_bias_demo``: select wallets on the first half of the horizon by
   (a) naive top-N in-sample PnL and (b) the BH edge test, then measure both sets
   out-of-sample on the second half.

Assumptions:
    * In-sample = markets that resolved before the split; out-of-sample = units
      whose first entry is at or after the split. Phase 3 replaces this single
      split with the full epoch walk-forward.
    * The stake cap is computed from in-sample units only.

Failure mode guarded against:
    * Believing a statistic because it looks good: these experiments measure the
      false-positive behaviour of the machinery on data where the truth is known.
    * Non-reproducible results: output is a pure function of (config, seeds) and is
      serialised with sorted keys and fixed rounding (byte-identical reports).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from polybot.config import Config
from polybot.data.synthetic import DAY, T0, World, generate_world
from polybot.stats.calibration import ks_uniform, wilson_ci
from polybot.stats.edge import build_units, stake_cap, wallet_edges
from polybot.stats.fdr import benjamini_hochberg


def _r(x: float, nd: int = 6) -> float | None:
    x = float(x)
    return None if not np.isfinite(x) else round(x, nd)


def null_calibration(
    cfg: Config, seeds: list[int], price_model: str | None = None
) -> dict[str, Any]:
    """Run the null world over ``seeds``; summarise p-value calibration."""
    sc, st, fl = cfg.synthetic, cfg.stats, cfg.filters
    ks_p: list[float] = []
    pooled: list[np.ndarray] = []
    bh_any = 0
    bh_total = 0
    small_z_rej = small_x_rej = small_n = 0
    for s in seeds:
        w = generate_world(sc, s, "null", price_model)
        u = build_units(w.trades, w.winners).units
        cap = stake_cap(u, st.stake_cap_quantile)
        e = wallet_edges(
            u,
            cap,
            small_n_exact_threshold=st.small_n_exact_threshold,
            seed=s,
            n_mc=sc.calibration_mc_sims,
        )
        elig = e.loc[e["n_markets"] >= fl.min_resolved_markets]
        pv = elig["p_value"].to_numpy(dtype=np.float64)
        ks_p.append(ks_uniform(pv).p_value)
        pooled.append(pv)
        bh = benjamini_hochberg(pv, st.fdr_q)
        bh_total += bh.n_rejected
        bh_any += int(bh.n_rejected > 0)
        small = e.loc[e["n_markets"] < st.small_n_exact_threshold]
        small_n += len(small)
        small_z_rej += int((norm.sf(small["z"].to_numpy(dtype=np.float64)) < st.alpha).sum())
        small_x_rej += int((small["p_value"].to_numpy(dtype=np.float64) < st.alpha).sum())

    n = len(seeds)
    ks_arr = np.asarray(ks_p)
    ks_fail = int((ks_arr <= 0.01).sum())
    pooled_ks = ks_uniform(np.concatenate(pooled))
    allp = np.concatenate(pooled)
    lo, hi = wilson_ci(ks_fail, n)
    blo, bhi = wilson_ci(bh_any, n)
    return {
        "experiment": "null_calibration",
        "price_model": price_model or cfg.synthetic.price_model,
        "n_seeds": n,
        "eligible_wallets_per_seed_mean": _r(float(np.mean([p.size for p in pooled])), 1),
        "per_seed_ks": {
            "fail_rate_at_0.01": _r(ks_fail / n),
            "fail_rate_wilson95": [_r(lo), _r(hi)],
            "expected_if_calibrated": 0.01,
            "ks_pvalues_uniform_ks_p": _r(ks_uniform(ks_arr).p_value),
        },
        "pooled_ks": {
            "n": pooled_ks.n,
            "stat": _r(pooled_ks.statistic),
            "p": _r(pooled_ks.p_value),
        },
        "rejection_rate": {
            "at_0.05": _r(float(np.mean(allp < 0.05))),
            "at_0.01": _r(float(np.mean(allp < 0.01))),
        },
        "bh": {
            "q": st.fdr_q,
            "seeds_with_any_discovery": bh_any,
            "rate": _r(bh_any / n),
            "rate_wilson95": [_r(blo), _r(bhi)],
            "bound_under_global_null": st.fdr_q,
            "total_false_discoveries": bh_total,
        },
        "small_n": {
            "wallets": small_n,
            "z_test_reject_rate_at_alpha": _r(small_z_rej / small_n) if small_n else None,
            "exact_mc_reject_rate_at_alpha": _r(small_x_rej / small_n) if small_n else None,
            "alpha": st.alpha,
        },
    }


def _oos_summary(units: pd.DataFrame, wallets: list[str], cap: float) -> dict[str, Any]:
    sel = units.loc[units["wallet"].isin(wallets)]
    if sel.empty:
        return {
            "n_wallets": len(wallets),
            "n_units": 0,
            "roi": None,
            "pooled_z": None,
            "frac_positive_edge": None,
        }
    p = sel["p_bar"].to_numpy(dtype=np.float64)
    y = sel["y"].to_numpy(dtype=np.float64)
    w = np.minimum(sel["cost"].to_numpy(dtype=np.float64), cap)
    num = w * (y - p)
    den = w * w * p * (1 - p)
    per_w = pd.DataFrame({"wallet": sel["wallet"].to_numpy(), "num": num}).groupby("wallet")
    return {
        "n_wallets": len(wallets),
        "n_units": len(sel),
        "roi": _r(sel["pnl"].sum() / sel["cost"].sum()),
        "pooled_z": _r(num.sum() / np.sqrt(den.sum())),
        "frac_positive_edge": _r(float((per_w["num"].sum() > 0).mean())),
    }


def selection_bias_demo(
    cfg: Config, seed: int, world: World = "null", price_model: str | None = None
) -> dict[str, Any]:
    """Naive top-N by in-sample PnL vs BH selection, judged out-of-sample."""
    sc, st, fl = cfg.synthetic, cfg.stats, cfg.filters
    wd = generate_world(sc, seed, world, price_model)
    split = T0 + (sc.horizon_days // 2) * DAY
    allu = build_units(wd.trades, wd.winners).units
    end = wd.markets.set_index("market")["end_ts"]
    m_end = allu["market"].map(end).to_numpy()
    ins = allu.loc[m_end < split]
    oos = allu.loc[allu["first_ts"].to_numpy() >= split]

    cap = stake_cap(ins, st.stake_cap_quantile)
    e = wallet_edges(
        ins,
        cap,
        small_n_exact_threshold=st.small_n_exact_threshold,
        seed=seed,
        n_mc=sc.calibration_mc_sims,
    )
    naive = e.sort_values(["total_pnl", "wallet"], ascending=[False, True]).head(st.naive_top_n)
    elig = e.loc[e["n_markets"] >= fl.min_resolved_markets].reset_index(drop=True)
    bh = benjamini_hochberg(elig["p_value"].to_numpy(dtype=np.float64), st.fdr_q)
    bh_sel = elig.loc[bh.reject]

    arche = wd.wallets.set_index("wallet")["archetype"]
    out: dict[str, Any] = {
        "experiment": "selection_bias_demo",
        "world": world,
        "seed": seed,
        "price_model": wd.price_model,
        "n_eligible": len(elig),
    }
    for name, sel in (("naive_top_pnl", naive), ("bh", bh_sel)):
        ws = sorted(sel["wallet"].tolist())
        ins_roi = sel["total_pnl"].sum() / sel["total_cost"].sum() if len(sel) else np.nan
        block = {"in_sample_roi": _r(ins_roi), "oos": _oos_summary(oos, ws, cap)}
        if world == "skilled":
            n_sk = int((arche.loc[ws] == "skilled").sum()) if ws else 0
            elig_sk = int((arche.loc[elig["wallet"]] == "skilled").sum())
            block["skilled_selected"] = n_sk
            block["false_discovery_proportion"] = _r(1 - n_sk / len(ws)) if ws else None
            block["skilled_eligible"] = elig_sk
        out[name] = block
    return out


def selection_bias_over_seeds(
    cfg: Config, seeds: list[int], world: World, price_model: str | None = None
) -> dict[str, Any]:
    runs = [selection_bias_demo(cfg, s, world, price_model) for s in seeds]

    def agg(sel: str, path: tuple[str, ...]) -> dict[str, Any]:
        vals = []
        for r in runs:
            v: Any = r[sel]
            for k in path:
                v = v[k]
            if v is not None:
                vals.append(float(v))
        if not vals:
            return {"n": 0, "mean": None, "sd": None}
        return {"n": len(vals), "mean": _r(float(np.mean(vals))), "sd": _r(float(np.std(vals)))}

    summary: dict[str, Any] = {
        "experiment": "selection_bias_over_seeds",
        "world": world,
        "n_seeds": len(seeds),
        "price_model": price_model or cfg.synthetic.price_model,
    }
    for sel in ("naive_top_pnl", "bh"):
        summary[sel] = {
            "in_sample_roi": agg(sel, ("in_sample_roi",)),
            "oos_roi": agg(sel, ("oos", "roi")),
            "oos_pooled_z": agg(sel, ("oos", "pooled_z")),
            "oos_frac_positive_edge": agg(sel, ("oos", "frac_positive_edge")),
            "n_selected": agg(sel, ("oos", "n_wallets")),
        }
        if world == "skilled":
            summary[sel]["false_discovery_proportion"] = agg(sel, ("false_discovery_proportion",))
            summary[sel]["skilled_selected"] = agg(sel, ("skilled_selected",))
            summary[sel]["skilled_eligible"] = agg(sel, ("skilled_eligible",))
    return summary


# --- Phase 5: full-pipeline acceptance (false-promotion rate, power) -------------------


def _acceptance_one(args: tuple[Config, int, str, float, str | None]) -> dict[str, Any]:
    from polybot.backtest import run_backtest
    from polybot.config import config_hash
    from polybot.data.synthetic import generate_world
    from polybot.execution.replay import drift_price_fn

    cfg, seed, world, latency_ratio, price_model = args
    sc = cfg.synthetic
    wd = generate_world(sc, seed, world, price_model)  # type: ignore[arg-type]
    half_life = cfg.execution.latency_L_s / latency_ratio  # d/h = latency_ratio at 1xL
    start = T0 + cfg.walkforward.trailing_window_days * DAY
    end = T0 + sc.horizon_days * DAY
    r = run_backtest(
        wd.trades,
        wd.wf_markets,
        cfg,
        start_ts=start,
        end_ts=end,
        price_at=drift_price_fn(wd.markets, half_life),
        mode="synthetic",
        seed=seed,
        config_hash=config_hash(cfg),
        admissibility="simulation",
        lazy_placebo=True,
    )
    return {
        "seed": seed,
        "promoted": r.promoted,
        "gates": {g.name: g.passed for g in r.gates},
        "placebo_computed": r.placebo_computed,
        "settled_trades": len(r.evidence.trades),
        "roi": None
        if r.evidence.trades.empty
        else _r(float(r.evidence.trades["net_pnl"].sum() / r.evidence.trades["stake"].sum())),
    }


def acceptance(
    cfg: Config,
    seeds: list[int],
    world: World,
    latency_ratio: float,
    price_model: str | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    """Run the FULL pipeline + all paper gates (simulation admissibility) over seeds."""
    jobs = [(cfg, s, world, latency_ratio, price_model) for s in seeds]
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=workers) as pool:
            runs = list(pool.map(_acceptance_one, jobs))
    else:
        runs = [_acceptance_one(j) for j in jobs]
    n = len(runs)
    k = sum(r["promoted"] for r in runs)
    lo, hi = wilson_ci(k, n)
    names = list(runs[0]["gates"]) if runs else []
    return {
        "experiment": "acceptance",
        "world": world,
        "latency_ratio_d_over_h": latency_ratio,
        "price_model": price_model or cfg.synthetic.price_model,
        "n_seeds": n,
        "promoted": k,
        "promotion_rate": _r(k / n) if n else None,
        "promotion_rate_wilson95": [_r(lo), _r(hi)],
        "gate_pass_rate": {g: _r(float(np.mean([r["gates"][g] for r in runs]))) for g in names},
        "seeds_with_settled_trades": sum(1 for r in runs if r["settled_trades"]),
        "mean_roi_when_traded": _r(float(np.mean([r["roi"] for r in runs if r["roi"] is not None])))
        if any(r["roi"] is not None for r in runs)
        else None,
        "placebo_computed": sum(r["placebo_computed"] for r in runs),
        "runs": runs,
    }
