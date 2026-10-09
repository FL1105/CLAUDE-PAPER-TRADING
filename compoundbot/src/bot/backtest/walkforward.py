"""Walk-forward research harness: pre-registered grids, expanding window, OOS stitching,
trial logging, holdout split, placebo, and automated KEEP/REJECT verdicts (evidence gate E1-E6).

Parameters are chosen per test window using ONLY data before that window. Each parameter
combination is backtested once over the whole development sample; because strategies and
the engine are causal, truncating a result at a date equals running the backtest up to it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from bot.backtest import metrics, stats
from bot.backtest.engine import BacktestResult, rebalance_dates, run_backtest
from bot.config import Costs, Risk, Universe
from bot.strategies import Spec, split_universe

REGIMES = {
    "2008-09 crisis": ("2007-10-09", "2009-03-09"),
    "2013 taper": ("2013-05-22", "2013-09-05"),
    "2015-16 selloff": ("2015-06-01", "2016-02-11"),
    "2018 Q4": ("2018-10-01", "2018-12-24"),
    "2020 crash": ("2020-02-19", "2020-04-30"),
    "2022 rates": ("2022-01-03", "2022-10-14"),
}


# ----------------------------------------------------------------------------- logging
def log_trial(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=float) + "\n")


def trial_counts(path: Path) -> tuple[int, float]:
    """(number of distinct trials, variance of annual Sharpe across them) for deflated Sharpe."""
    if not path.exists():
        return 1, 0.25
    seen: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("stage") == "trial":
            seen[f'{r["strategy"]}|{json.dumps(r["params"], sort_keys=True)}|{r.get("data_id", "")}'] = r["sharpe"]
    if len(seen) < 2:
        return max(len(seen), 1), 0.25
    return len(seen), max(float(np.var(list(seen.values()), ddof=1)), 1e-4)


# ----------------------------------------------------------------------------- data split
def split_holdout(panel: dict, years: float) -> tuple[dict, dict, pd.Timestamp]:
    idx = panel["close"].index
    cut = idx[-1] - pd.DateOffset(years=int(years)) if years else idx[-1]
    cut = idx[idx.searchsorted(cut)]
    dev = {k: v.loc[: cut - pd.Timedelta(days=1)] if v is not None else None for k, v in panel.items()}
    hold = {k: v.loc[cut:] if v is not None else None for k, v in panel.items()}
    return dev, hold, cut


def key(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


@dataclass
class StrategyOutcome:
    name: str
    trials: dict[str, BacktestResult]
    trials_x2: dict[str, BacktestResult]
    oos_net: pd.Series
    oos_net_x2: pd.Series
    oos_gross: pd.Series
    oos_cost: pd.Series
    oos_turnover: pd.Series
    oos_orders: pd.Series
    chosen: list[dict]
    final_params: dict
    weights: pd.DataFrame
    placebo_sharpes: np.ndarray = field(default_factory=lambda: np.array([]))
    criteria: list[dict] = field(default_factory=list)
    verdict: str = "REJECT"


def run_trials(spec: Spec, panel: dict, universe: Universe, risk: Risk, costs: Costs,
               exec_mode: str, cost_multiplier: float, tag: str) -> tuple[dict, dict]:
    parts = split_universe(panel["close"], universe)
    cols = parts[spec.universe]
    sub = {"close": panel["close"][cols], "open": panel["open"][cols], "cash": panel["cash"],
           "volume": panel["volume"][cols] if panel.get("volume") is not None else None}
    res, weights = {}, {}
    for p in spec.combos():
        w = spec.build(sub["close"], p, risk)
        weights[key(p)] = w
        res[key(p)] = run_backtest(
            sub, w, costs=costs, risk=risk, universe=universe,
            rebalance=rebalance_dates(w.index, spec.rebalance), exec_mode=exec_mode,
            cost_multiplier=cost_multiplier, tag=tag)
    return res, weights


def _first_valid(res: BacktestResult) -> pd.Timestamp:
    nz = res.weights.abs().sum(axis=1) > 0
    return nz.idxmax() if nz.any() else res.net.index[0]


def walk_forward(trials: dict[str, BacktestResult], trials_x2: dict[str, BacktestResult],
                 cash: pd.Series, min_train_years: float, test_years: float):
    """Expanding window. Returns stitched OOS series and the list of chosen params per window."""
    any_res = next(iter(trials.values()))
    idx = any_res.net.index
    start = idx[0] + pd.DateOffset(years=int(min_train_years))
    edges = []
    t0 = idx[idx.searchsorted(start)]
    while t0 < idx[-1]:
        t1 = t0 + pd.DateOffset(years=int(test_years))
        edges.append((t0, min(t1, idx[-1] + pd.Timedelta(days=1))))
        t0 = idx[min(idx.searchsorted(t1), len(idx) - 1)]
        if t1 > idx[-1]:
            break
    parts = {k: [] for k in ("net", "net2", "gross", "cost", "turn", "orders")}
    chosen = []
    for a, b in edges:
        best, best_sr = None, -np.inf
        for k, r in trials.items():
            train = r.net.loc[: a - pd.Timedelta(days=1)]
            if train.abs().sum() == 0:
                continue
            s = metrics.sharpe(train, cash)
            if s > best_sr:
                best, best_sr = k, s
        if best is None:
            continue
        chosen.append({"from": str(a.date()), "params": json.loads(best), "train_sharpe": best_sr})
        r, r2 = trials[best], trials_x2[best]
        sl = slice(a, b - pd.Timedelta(days=1))
        parts["net"].append(r.net.loc[sl])
        parts["net2"].append(r2.net.loc[sl])
        parts["gross"].append(r.gross.loc[sl])
        parts["cost"].append(r.cost.loc[sl])
        parts["turn"].append(r.turnover.loc[sl])
        parts["orders"].append(r.orders.loc[sl])
    out = {k: pd.concat(v) if v else pd.Series(dtype=float) for k, v in parts.items()}
    return out, chosen


def _mode_params(chosen: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for c in chosen:
        counts[key(c["params"])] = counts.get(key(c["params"]), 0) + 1
    return json.loads(max(counts, key=counts.get)) if counts else {}


def neighbours(spec: Spec, params: dict) -> list[dict]:
    out = []
    for ax, vals in spec.grid.items():
        i = vals.index(params[ax])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                out.append({**params, ax: vals[j]})
    return out


def placebo_sharpes(weights: pd.DataFrame, sub_panel: dict, spec: Spec, universe: Universe,
                    risk: Risk, costs: Costs, exec_mode: str, oos_start: pd.Timestamp,
                    n: int, seed: int) -> np.ndarray:
    """Circularly shift the weight matrix in time (keeps turnover and exposure, breaks timing)."""
    rng = np.random.default_rng(seed)
    n_rows = len(weights)
    out = []
    lo, hi = 252, n_rows - 252
    reb = rebalance_dates(weights.index, spec.rebalance)
    for _ in range(n):
        shift = int(rng.integers(lo, hi))
        w = pd.DataFrame(np.roll(weights.to_numpy(), shift, axis=0), weights.index, weights.columns)
        r = run_backtest(sub_panel, w, costs=costs, risk=risk, universe=universe, rebalance=reb,
                         exec_mode=exec_mode)
        out.append(metrics.sharpe(r.net.loc[oos_start:], r.cash_ret.loc[oos_start:]))
    return np.array(out)


def evaluate(outcome: StrategyOutcome, spec: Spec, baselines: dict[str, BacktestResult],
             cash: pd.Series, risk: Risk, n_trials: int, sr_var: float, cover_min: float,
             n_boot: int, seed: int, dd_limit_extra: float = 0.05) -> None:
    r = outcome.oos_net
    sr = metrics.sharpe(r, cash)
    lo, hi = stats.bootstrap_sharpe_ci(r, n_boot=n_boot, seed=seed, cash=cash)
    dsr = stats.deflated_sharpe(r - cash.reindex(r.index).fillna(0.0), n_trials, sr_var)
    crit: list[dict] = []

    def add(cid, desc, value, thr, ok, ci=None, n=None):
        crit.append({"id": cid, "desc": desc, "value": value, "threshold": thr,
                     "pass": bool(ok), "ci": ci, "n": n})

    add("E1", "Net Sharpe 95% CI lower bound > 0 and Deflated Sharpe >= 0.90",
        f"SR {sr:.2f}, lo {lo:.2f}, DSR {dsr:.2f}", "lo>0 & DSR>=0.90", lo > 0 and dsr >= 0.90,
        (lo, hi), len(r))
    # E2: beat the best risk-adjusted baseline (incl. vol-targeted) and the shuffled-signal placebo
    base_sr = {n: metrics.sharpe(b.net.reindex(r.index).dropna(), cash) for n, b in baselines.items()
               if n != "cash"}
    best = max(base_sr, key=base_sr.get)
    bnet = baselines[best].net.reindex(r.index).dropna()
    pb = stats.paired_bootstrap_diff(r, bnet, n_boot=n_boot, seed=seed)
    cheaper = (metrics.cagr(r) >= metrics.cagr(bnet)
               and metrics.max_drawdown(r) <= 0.8 * metrics.max_drawdown(bnet))
    p_pl = stats.placebo_pvalue(sr, outcome.placebo_sharpes) if len(outcome.placebo_sharpes) else 1.0
    add("E2", f"Beats best baseline ({best}) p<0.10 or matches return with lower DD; "
        f"beats placebo p<0.10",
        f"dSR {pb['diff']:.2f} p={pb['p_value']:.2f}; placebo p={p_pl:.3f}",
        "p<0.10 (or cheaper DD) & placebo p<0.10",
        (pb["p_value"] < 0.10 or cheaper) and p_pl < 0.10, pb["ci"], len(r))
    # E3: costs x2 and cost cover for higher-turnover sleeves
    sr2 = metrics.sharpe(outcome.oos_net_x2, cash)
    yrs = len(r) / 252
    turns = float(outcome.oos_turnover.sum() / 2 / yrs)
    cover = float(outcome.oos_gross.sum() / outcome.oos_cost.sum()) if outcome.oos_cost.sum() > 0 else np.inf
    need_cover = turns > 12
    add("E3", f"Sharpe>0 at 2x costs; cost cover >= {cover_min:g} if turns/yr > 12",
        f"SR(x2) {sr2:.2f}; turns/yr {turns:.1f}; cover {cover:.1f}", f"SR2>0 & cover>={cover_min:g}",
        sr2 > 0 and (cover >= cover_min or not need_cover), None, len(r))
    # E4: drawdown with the ladder active, worst calendar year
    mdd = metrics.max_drawdown(r)
    ys = metrics.period_returns(r, "Y")
    worst_y = float(ys.min()) if len(ys) else 0.0
    lim = risk.drawdown_halt_at + dd_limit_extra
    add("E4", "OOS max DD (ladder on) <= halt+5pts; worst calendar year >= -10%",
        f"maxDD {mdd:.1%}; worst year {worst_y:.1%}", f"<= {lim:.0%}, >= -10%",
        mdd <= lim and worst_y >= -0.10, None, len(ys))
    # E5: parameter plateau (neighbours within 30% of chosen on the development sample)
    fp = outcome.final_params
    chosen_sr = metrics.sharpe(outcome.trials[key(fp)].net, cash)
    nb = [metrics.sharpe(outcome.trials[key(p)].net, cash) for p in neighbours(spec, fp)]
    plateau = chosen_sr > 0 and all(s >= 0.7 * chosen_sr for s in nb)
    add("E5", "Neighbouring params (+/-1 grid step) keep Sharpe within 30% of chosen",
        f"chosen {chosen_sr:.2f}; neighbours {', '.join(f'{s:.2f}' for s in nb) or 'n/a'}",
        ">= 0.7x chosen", plateau, None, len(nb))
    # E6: no single year dominates; regimes not hurt
    tot = float((1 + r).prod() - 1)
    share = float(ys.clip(lower=0).max() / ys.clip(lower=0).sum()) if ys.clip(lower=0).sum() > 0 else 1.0
    concentrated = tot > 0 and share > 0.40
    covered, okc = [], []
    for name, (a, b) in REGIMES.items():
        seg = r.loc[a:b]
        if len(seg) > 20:
            covered.append(name)
            okc.append(float((1 + seg).prod() - 1) >= -0.02)
    need = min(3, len(covered))
    add("E6", "No year > 40% of positive-year return; >=3 covered regimes flat or better",
        f"best-year share {share:.0%}; regimes ok {sum(okc)}/{len(covered)}",
        "<=40%, >=3 (or all if fewer)", (not concentrated) and sum(okc) >= need,
        None, len(covered))
    outcome.criteria = crit
    outcome.verdict = "KEEP" if all(c["pass"] for c in crit) else "REJECT"


def research(panel: dict, universe: Universe, risk: Risk, costs: Costs, registry: dict[str, Spec],
             wf_cfg: dict, exp_path: Path, tag: str, data_id: str = "", n_placebo: int = 200,
             n_boot: int = 500, cover_min: float = 2.0, only: list[str] | None = None) -> dict:
    """Run the full research procedure on the DEVELOPMENT sample (holdout already removed)."""
    from bot.backtest.baselines import run_baselines

    exec_mode, seed = wf_cfg["exec_mode"], wf_cfg["seed"]
    cash = panel["cash"].iloc[:, 0].pct_change().fillna(0.0)
    base = run_baselines(panel, universe, risk, costs, tag=tag, exec_mode=exec_mode)
    out: dict[str, StrategyOutcome] = {}
    for name, spec in registry.items():
        if only and name not in only:
            continue
        t1, w = run_trials(spec, panel, universe, risk, costs, exec_mode, 1.0, tag)
        t2, _ = run_trials(spec, panel, universe, risk, costs, exec_mode, 2.0, tag)
        for k, r in t1.items():
            log_trial(exp_path, {"stage": "trial", "strategy": name, "params": json.loads(k),
                                 "sharpe": metrics.sharpe(r.net, cash), "n_days": len(r.net),
                                 "tag": tag, "data_id": data_id})
        stitched, chosen = walk_forward(t1, t2, cash, wf_cfg["min_train_years"], wf_cfg["test_years"])
        if not len(stitched["net"]):
            continue
        fp = _mode_params(chosen)
        oc = StrategyOutcome(
            name, t1, t2, stitched["net"], stitched["net2"], stitched["gross"], stitched["cost"],
            stitched["turn"], stitched["orders"], chosen, fp, w[key(fp)])
        parts = split_universe(panel["close"], universe)[spec.universe]
        sub = {"close": panel["close"][parts], "open": panel["open"][parts], "cash": panel["cash"],
               "volume": None}
        oc.placebo_sharpes = placebo_sharpes(
            oc.weights, sub, spec, universe, risk, costs, exec_mode, oc.oos_net.index[0],
            n_placebo, seed) if n_placebo else np.array([])
        out[name] = oc
    n_trials, sr_var = trial_counts(exp_path)
    for name, oc in out.items():
        evaluate(oc, registry[name], base, cash, risk, n_trials, sr_var, cover_min, n_boot, seed)
        log_trial(exp_path, {"stage": "verdict", "strategy": name, "verdict": oc.verdict,
                             "params": oc.final_params, "tag": tag, "data_id": data_id})
    return {"outcomes": out, "baselines": base, "n_trials": n_trials, "sr_var": sr_var,
            "cash": cash, "tag": tag}
