"""The 12 paper promotion gates (S1 -> S2). Each gate is a pure function -> GateResult.

Thresholds come from ``config.gates`` (and a few sibling sections); they are fixed
before evaluation and mirrored with reasoning in docs/GATES.md.

Admissibility:
    * ``strict`` (default, the only mode for real decisions): gate 1 requires
      evidence_mode == "live_forward". Backtests and synthetic runs cannot pass.
    * ``simulation``: used ONLY by the synthetic acceptance tests to measure the
      false-promotion rate and power of gates 2-12 plus gate 1's duration check.
      Results computed this way are labeled and never admissible.

Failure mode guarded against: promotion on luck, leakage, cost blindness, or
tuning. A candidate promotes only if ALL gates pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from polybot.config import Config
from polybot.gates import metrics as M  # noqa: N812
from polybot.stats.bootstrap import cluster_bootstrap_roi

DAY = 86_400
Admissibility = Literal["strict", "simulation"]


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    observed: Any
    threshold: Any
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "passed", bool(self.passed))  # never a numpy bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed": self.observed,
            "threshold": self.threshold,
            "detail": self.detail,
        }


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def g1_forward(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    days = (ev.end_ts - ev.start_ts) / DAY
    mode_ok = ev.mode == "live_forward" or adm == "simulation"
    ok = mode_ok and days >= cfg.gates.min_forward_days
    why = "" if mode_ok else f"evidence mode '{ev.mode}' is not admissible; only live_forward is"
    return GateResult(
        "1_forward_not_simulated",
        ok,
        {"mode": ev.mode, "days": _r(days, 1)},
        {"mode": "live_forward", "min_days": cfg.gates.min_forward_days},
        why or f"{days:.1f} days of forward evidence",
    )


def g2_sample(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    g, t = cfg.gates, ev.trades
    n, mk = len(t), int(t["market"].nunique()) if len(t) else 0
    ne = int(t["epoch"].nunique()) if len(t) else 0
    ok = n >= g.min_settled_trades and mk >= g.min_distinct_markets and ne >= g.min_epochs
    return GateResult(
        "2_sample_size",
        ok,
        {"settled_trades": n, "markets": mk, "epochs": ne},
        {
            "settled_trades": g.min_settled_trades,
            "markets": g.min_distinct_markets,
            "epochs": g.min_epochs,
        },
        "settled trades / distinct markets / epochs",
    )


def g3_net_edge(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    t = ev.trades
    if t.empty or float(t["stake"].sum()) <= 0:
        return GateResult("3_net_edge_credible", False, None, "> 0", "no settled trades")
    b = cluster_bootstrap_roi(
        t["net_pnl"],
        t["stake"],
        t["market"].astype(str),
        n_resamples=cfg.stats.bootstrap_resamples,
        seed=cfg.seed,
        ci_level=cfg.gates.bootstrap_ci_level,
    )
    ok = b.roi > 0 and b.lower > 0
    return GateResult(
        "3_net_edge_credible",
        ok,
        {"roi": _r(b.roi), "ci_lower": _r(b.lower)},
        {"roi": "> 0", "ci_lower": "> 0"},
        f"one-sided {cfg.gates.bootstrap_ci_level:.0%} market-clustered bootstrap, "
        f"{b.n_resamples} resamples, {b.n_clusters} markets",
    )


def g4_placebo(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    r = ev.placebo_strategy_roi if ev.placebo_strategy_roi is not None else M.roi(ev.trades)
    if ev.placebo_rois is None or not ev.placebo_rois or r is None:
        return GateResult(
            "4_selection_adds_value",
            False,
            {"roi": _r(r)},
            None,
            "placebo distribution not computed",
        )
    thr = float(np.percentile(ev.placebo_rois, cfg.placebo.required_percentile))
    pct = float(np.mean(np.asarray(ev.placebo_rois) < r) * 100)
    return GateResult(
        "4_selection_adds_value",
        r > thr,
        {"roi": _r(r), "placebo_percentile": _r(pct, 1)},
        {f"p{cfg.placebo.required_percentile:g}_of_placebo": _r(thr)},
        f"{len(ev.placebo_rois)} placebo draws of random matched eligible wallets",
    )


def g5_persistence(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    g = cfg.gates
    z, f = ev.pooled_oos_z, ev.frac_wallets_positive_oos
    ok = (
        z is not None
        and f is not None
        and z > g.persistence_min_pooled_z
        and f >= g.persistence_min_positive_frac
    )
    return GateResult(
        "5_persistence",
        ok,
        {"pooled_oos_z": _r(z), "frac_positive": _r(f)},
        {
            "pooled_oos_z": f"> {g.persistence_min_pooled_z}",
            "frac_positive": f">= {g.persistence_min_positive_frac}",
        },
        "selected wallets' out-of-sample edge",
    )


def g6_cost(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    s = ev.stress_roi
    return GateResult(
        "6_cost_robustness",
        s is not None and s > 0,
        {"stress_roi": _r(s)},
        "> 0",
        f"{cfg.fees.stress_multiplier:g}x fees AND 2x slippage AND 2x latency",
    )


def g7_latency(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    lim = cfg.gates.latency_must_survive_multiplier
    need = [m for m in cfg.execution.shadow_multipliers if m <= lim]
    vals = {m: ev.latency_roi.get(m) for m in need}
    ok = bool(need) and all(v is not None and v > 0 for v in vals.values())
    curve = {f"{m}xL": _r(v) for m, v in sorted(ev.latency_roi.items())}
    return GateResult(
        "7_latency_robustness",
        ok,
        curve,
        f"> 0 up to {lim}xL",
        "ROI vs latency (shadow fills) must not cross zero before 2xL",
    )


def g8_lucky(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    g, t = cfg.gates, ev.trades
    if t.empty:
        return GateResult("8_not_a_lucky_streak", False, None, None, "no trades")
    r_k = M.roi_without_top(t, g.remove_top_k_trades)
    cm, cw = M.concentration(t, "market"), M.concentration(t, "wallet")
    ok = (
        r_k is not None
        and r_k > 0
        and cm <= g.max_single_market_pnl_share
        and cw <= g.max_single_wallet_pnl_share
    )
    return GateResult(
        "8_not_a_lucky_streak",
        ok,
        {
            f"roi_without_top_{g.remove_top_k_trades}": _r(r_k),
            "top_market_share": _r(cm),
            "top_wallet_share": _r(cw),
        },
        {
            "roi_without_top": "> 0",
            "top_market_share": g.max_single_market_pnl_share,
            "top_wallet_share": g.max_single_wallet_pnl_share,
        },
        "concentration shares are of total PnL (1.0 when total PnL <= 0)",
    )


def g9_fill(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    fr = ev.signals_filled / ev.signals_reached_book if ev.signals_reached_book else None
    ms, cap = ev.median_slippage, cfg.execution.max_median_slippage
    ok = fr is not None and fr >= cfg.gates.min_fill_rate and ms is not None and ms <= cap
    return GateResult(
        "9_fillability",
        ok,
        {"fill_rate": _r(fr), "median_slippage": _r(ms)},
        {"fill_rate": f">= {cfg.gates.min_fill_rate}", "median_slippage": f"<= {cap}"},
        f"{ev.signals_filled}/{ev.signals_reached_book} signals that reached the book",
    )


def g10_drawdown(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    g, t = cfg.gates, ev.trades
    dd = M.max_drawdown(t, ev.bankroll)
    if t.empty:
        return GateResult("10_drawdown", False, None, None, "no trades")
    # Streaks are counted per MARKET (unit of observation, DECISIONS D1): several copies
    # in one market share one outcome, so trade-level streaks would be inflated.
    mk = t.groupby("market").agg(pnl=("net_pnl", "sum"), r=("resolved_ts", "max"))
    wins = mk.sort_values(["r"], kind="mergesort")["pnl"].to_numpy() > 0
    streak = M.longest_losing_streak(wins)
    lim = M.streak_percentile(
        len(wins), float(wins.mean()), g.losing_streak_percentile, sims=2000, seed=cfg.seed
    )
    ok = dd <= g.max_drawdown_frac and streak <= lim
    return GateResult(
        "10_drawdown",
        ok,
        {"max_drawdown": _r(dd), "longest_losing_streak": streak},
        {
            "max_drawdown": g.max_drawdown_frac,
            f"streak_p{g.losing_streak_percentile:g}": _r(lim, 1),
        },
        "settled equity; per-market losing streak vs what the win rate gives by chance",
    )


def g11_stability(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    g = cfg.gates
    tab = M.epoch_table(ev.trades, ev.bankroll)
    if not tab:
        return GateResult("11_stability", False, None, None, "no epochs with trades")
    pos = float(np.mean([e["pnl"] > 0 for e in tab]))
    worst = min(e["pnl_frac_bankroll"] for e in tab)
    ok = pos >= g.min_positive_epoch_frac and worst >= -g.max_epoch_loss_frac
    return GateResult(
        "11_stability",
        ok,
        {"positive_epoch_frac": _r(pos), "worst_epoch_frac_bankroll": _r(worst)},
        {"positive_epoch_frac": g.min_positive_epoch_frac, "worst_epoch": -g.max_epoch_loss_frac},
        f"{len(tab)} epochs",
    )


def g12_integrity(ev: M.Evidence, cfg: Config, adm: Admissibility) -> GateResult:
    same = bool(ev.config_hash_start) and ev.config_hash_start == ev.config_hash_now
    ok = ev.leakage_failures == 0 and ev.recent_drift_errors == 0 and same
    return GateResult(
        "12_data_integrity",
        ok,
        {
            "leakage_failures": ev.leakage_failures,
            "schema_drift_last_7d": ev.recent_drift_errors,
            "config_unchanged": same,
        },
        {"leakage_failures": 0, "schema_drift_last_7d": 0, "config_unchanged": True},
        "no mid-test tuning, no leakage, clean data",
    )


EXPLAIN = {
    "1_forward_not_simulated": "Only live forward paper trading with real order books counts. "
    "Backtests and simulations can never pass this gate, and it needs at least 30 days.",
    "2_sample_size": "Enough settled trades, across enough different markets and epochs, to "
    "tell a few percent of edge from zero.",
    "3_net_edge_credible": "After fees and slippage, the LOWER end of a 95% confidence range "
    "for ROI is still above zero, with trades in the same market resampled together.",
    "4_selection_adds_value": "Copying the selected wallets beats 95% of random, similar "
    "wallets copied the same way. If not, the selection step adds nothing.",
    "5_persistence": "The selected wallets keep their edge out of sample (pooled z > 1.64) "
    "and most of them (>= 55%) stay positive. Luck fades; skill persists.",
    "6_cost_robustness": "Still profitable with fees doubled, spread doubled and latency "
    "doubled at the same time.",
    "7_latency_robustness": "Edge must survive being 2x slower than our base latency. Edge "
    "that only exists at zero latency is not ours to capture.",
    "8_not_a_lucky_streak": "Still profitable without the 3 best trades; no single market "
    "> 10% and no single wallet > 35% of total profit.",
    "9_fillability": "We actually got filled on >= 85% of signals that reached the book, with "
    "median slippage within the cap.",
    "10_drawdown": "Worst peak-to-trough fall <= 20% of the bankroll, and the longest losing "
    "streak (per market) is something the win rate could plausibly produce.",
    "11_stability": "Profitable in >= 60% of epochs and no epoch loses more than 5% of the "
    "bankroll.",
    "12_data_integrity": "No leakage, no schema errors in the last week, and the config has "
    "not changed since the forward test began (no mid-test tuning).",
}


GATES: tuple[Callable[[M.Evidence, Config, Admissibility], GateResult], ...] = (
    g1_forward,
    g2_sample,
    g3_net_edge,
    g4_placebo,
    g5_persistence,
    g6_cost,
    g7_latency,
    g8_lucky,
    g9_fill,
    g10_drawdown,
    g11_stability,
    g12_integrity,
)


def evaluate(
    ev: M.Evidence, cfg: Config, admissibility: Admissibility = "strict"
) -> list[GateResult]:
    return [g(ev, cfg, admissibility) for g in GATES]


def all_pass(results: list[GateResult]) -> bool:
    return all(r.passed for r in results)
