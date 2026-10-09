"""Learn real trading costs from fills, conservatively.

Why: the whole plan rests on small net wins, so the cost model is the most valuable thing to get right.
Inputs : runs/<date>/run.json fills (price, reference price = session open, fee). Fills from the local
         simulator are circular (they equal the model by construction) and are excluded by default;
         only broker-paper or live fills inform the estimate.
Method : per asset class, 10%-trimmed mean of total per-side cost in bps (price slippage + explicit fee),
         bootstrap CI, shrunk toward the current model with k pseudo-observations.
Policy : increases (reality is worse than modelled) may be applied automatically; decreases (which make
         strategies look better) need many fills, a CI entirely below the model, and an operator signature.
         Changes are bounded to [0.5x, 2x] of the current model per update. Every change is logged.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy import stats as st

from bot.config import CFG, Costs, Universe, load_universe

K_PRIOR = 30
MIN_N_INCREASE = 10
MIN_N_DECREASE = 60
TRIM = 0.10
BOUNDS = (0.5, 2.0)


@dataclass
class ClassEstimate:
    cls: str
    n: int
    obs_total_bps: float
    ci_lo: float
    ci_hi: float
    obs_slip_bps: float
    obs_fee_bps: float
    modelled_total_bps: float
    shrunk_total_bps: float
    proposed_total_bps: float
    proposed_spread_slippage_bps: float
    per_side_bps: float
    action: str  # increase | decrease | hold | insufficient
    reason: str
    fill_rate: float = float("nan")


# ----------------------------------------------------------------------------- data
def collect_fills(runs_dir: Path, universe: Universe | None = None, include_sim: bool = False) -> pd.DataFrame:
    universe = universe or load_universe()
    rows = []
    for p in sorted(Path(runs_dir).glob("*/run.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        broker = r.get("broker", "")
        if broker.startswith("PaperLocal") and not include_sim:
            continue
        for f in r.get("fills", []) + r.get("queued", []):
            rows.append({"date": r["run_id"], "broker": broker, **{k: f.get(k) for k in (
                "symbol", "side", "qty", "price", "ref_price", "fee_usd", "status")}})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["cls"] = df["symbol"].map(lambda s: universe.cls_of(s) if s in universe.symbols else None)
    return df


def fill_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add slippage_bps (positive = cost), fee_bps, total_bps for usable fills."""
    ok = df[df["status"].isin(["filled", "partial"]) & (df["qty"] > 0) & (df["ref_price"] > 0) &
            (df["price"] > 0)].copy()
    buy = ok["side"] == "buy"
    ok["slip_bps"] = np.where(buy, ok["price"] / ok["ref_price"] - 1, ok["ref_price"] / ok["price"] - 1) * 1e4
    ok["notional"] = ok["qty"] * ok["price"]
    ok["fee_bps"] = ok["fee_usd"].fillna(0.0) / ok["notional"] * 1e4
    ok["total_bps"] = ok["slip_bps"] + ok["fee_bps"]
    return ok


def _trim_boot(x: np.ndarray, n_boot: int, seed: int) -> tuple[float, float, float]:
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    est = float(st.trim_mean(x, TRIM))
    if len(x) < 5:
        return est, float("-inf"), float("inf")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    bs = np.sort(x[idx], axis=1)
    k = int(TRIM * len(x))
    boots = bs[:, k: len(x) - k].mean(axis=1) if len(x) - 2 * k > 0 else bs.mean(axis=1)
    return est, float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))


# ----------------------------------------------------------------------------- estimate
def estimate(df: pd.DataFrame, costs: Costs, n_boot: int = 1000, seed: int = 7) -> list[ClassEstimate]:
    out = []
    ok = fill_metrics(df) if len(df) else df
    for cls in sorted(costs.per_side_bps):
        modelled = costs.per_side_bps[cls] + costs.spread_slippage_bps[cls]
        x = ok[ok["cls"] == cls] if len(ok) else ok
        n = len(x)
        allc = df[df["cls"] == cls] if len(df) else df
        fr = float((allc["status"].isin(["filled", "partial"])).mean()) if len(allc) else float("nan")
        if n == 0:
            out.append(ClassEstimate(cls, 0, float("nan"), float("nan"), float("nan"), float("nan"),
                                     float("nan"), modelled, modelled, modelled,
                                     costs.spread_slippage_bps[cls], costs.per_side_bps[cls],
                                     "insufficient", "no usable broker fills yet", fr))
            continue
        tot, lo, hi = _trim_boot(x["total_bps"].to_numpy(float), n_boot, seed)
        shrunk = (n * tot + K_PRIOR * modelled) / (n + K_PRIOR)
        shrunk = float(np.clip(shrunk, BOUNDS[0] * modelled, BOUNDS[1] * modelled))
        action, reason = "hold", "within 10% of the model or too few fills"
        if n >= MIN_N_INCREASE and shrunk > 1.10 * modelled:
            action, reason = "increase", f"observed {tot:.1f} bps vs modelled {modelled:.1f} (n={n})"
        elif n >= MIN_N_DECREASE and shrunk < 0.90 * modelled and hi < modelled:
            action, reason = "decrease", f"CI [{lo:.1f}, {hi:.1f}] entirely below modelled {modelled:.1f} (n={n})"
        elif n < MIN_N_INCREASE:
            action, reason = "insufficient", f"only {n} usable fills (need {MIN_N_INCREASE})"
        proposed = shrunk if action in ("increase", "decrease") else modelled
        ps = costs.per_side_bps[cls]
        flag = ""
        if proposed < ps:
            flag = f" per_side_bps ({ps}) alone exceeds the proposed total; review that input"
        out.append(ClassEstimate(
            cls, n, tot, lo, hi, float(x["slip_bps"].mean()), float(x["fee_bps"].mean()), modelled, shrunk,
            proposed, max(proposed - ps, 0.0), ps, action, reason + flag, fr))
    return out


# ----------------------------------------------------------------------------- apply
def apply_estimates(ests: list[ClassEstimate], approved_by: str, only_increases: bool,
                    cfg_dir: Path | None = None, now: str | None = None) -> list[ClassEstimate]:
    """Write config/costs_learned.yaml (merged with what is there) and append to state history.
    only_increases=True is the unattended mode: decreases are left as proposals."""
    cfg_dir = Path(cfg_dir or CFG)
    path = cfg_dir / "costs_learned.yaml"
    cur = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cur = cur or {}
    spread = dict(cur.get("spread_slippage_bps") or {})
    applied = []
    for e in ests:
        if e.action == "increase" or (e.action == "decrease" and not only_increases):
            spread[e.cls] = round(e.proposed_spread_slippage_bps, 3)
            applied.append(e)
    if not applied:
        return []
    who = approved_by if approved_by else "auto-conservative"
    cur.update({"approved_by": who, "updated": now or time.strftime("%Y-%m-%d"),
                "spread_slippage_bps": spread})
    path.write_text(yaml.safe_dump(cur, sort_keys=False), encoding="utf-8")
    hist = cfg_dir.parent / "state" / "costs_history.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    with open(hist, "a", encoding="utf-8") as f:
        for e in applied:
            f.write(json.dumps({"ts": cur["updated"], "by": who, **asdict(e)}, default=float) + "\n")
    return applied


# ----------------------------------------------------------------------------- analysis
def order_size_table(costs: Costs, cls: str, sizes=(25, 50, 100, 250, 500, 1000, 2500)) -> list[dict]:
    """Round-trip cost in bps of notional, including fixed fees, for different order sizes."""
    side = costs.side_bps(cls)
    fixed = costs.commission_usd_per_order + costs.fixed_fee_usd_per_order
    return [{"order_usd": n, "round_trip_bps": 2 * side + 2 * fixed / n * 1e4} for n in sizes]


def breakeven_cost_multiplier(panel: dict, selected: dict, universe, risk, costs: Costs, exec_mode: str,
                              hi: float = 64.0, iters: int = 9) -> dict:
    """Largest multiple of the modelled costs at which the selected book's net Sharpe (excess of cash)
    is still positive. Answers 'how wrong can my fee estimate be before the edge is gone?'."""
    from bot.backtest import metrics
    from bot.backtest.engine import rebalance_dates, run_backtest
    from bot.backtest.portfolio_report import build_portfolio
    from bot.portfolio.construct import book_rebalance
    from bot.strategies import make_registry

    base, weights, _a, _s = build_portfolio(panel, selected, universe, risk, costs, exec_mode)
    from bot.config import load_strategy
    reb = rebalance_dates(weights.index, book_rebalance(selected, make_registry(load_strategy())))
    pan = {**panel, "volume": None}

    def sharpe_at(m: float) -> float:
        r = run_backtest(pan, weights, costs=costs, risk=risk, universe=universe, rebalance=reb,
                         exec_mode=exec_mode, cost_multiplier=m)
        return metrics.sharpe(r.net, r.cash_ret)

    s1 = sharpe_at(1.0)
    out = {"sharpe_x1": s1, "breakeven": None}
    if s1 <= 0:
        out["breakeven"] = 0.0
        return out
    if sharpe_at(hi) > 0:
        out["breakeven"] = float("inf")
        return out
    lo_m, hi_m = 1.0, hi
    for _ in range(iters):
        mid = (lo_m * hi_m) ** 0.5
        if sharpe_at(mid) > 0:
            lo_m = mid
        else:
            hi_m = mid
    out["breakeven"] = float(lo_m)
    return out


def calibration_md(ests: list[ClassEstimate], df: pd.DataFrame, costs: Costs, path: Path,
                   breakeven: dict | None = None, note: str = "") -> None:
    def f(x, d=1):
        return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}"

    lines = ["# Cost calibration", "",
             "Compares the cost model (`config/costs.yaml`) with what real broker fills cost. Total per-side cost = "
             "price slippage vs the session open + explicit fee, in bps of notional. Local-simulator fills are "
             "excluded (circular).", "", note, "",
             "| class | usable fills | fill rate | observed total [95% CI] | modelled | shrunk | action | why |",
             "|---|---|---|---|---|---|---|---|"]
    for e in ests:
        lines.append(f"| {e.cls} | {e.n} | {f(e.fill_rate * 100 if np.isfinite(e.fill_rate) else e.fill_rate, 0)}% | "
                     f"{f(e.obs_total_bps)} [{f(e.ci_lo)}, {f(e.ci_hi)}] | {f(e.modelled_total_bps)} | "
                     f"{f(e.shrunk_total_bps)} | **{e.action}** | {e.reason} |")
    lines += ["", "Policy: increases may be applied automatically; decreases need >= 60 fills, a CI entirely below the "
              "model and an operator signature (`python -m bot.cli costs --apply --by <you>`). Updates are bounded to "
              "0.5x-2x of the model.", ""]
    if breakeven:
        be = breakeven["breakeven"]
        if be == float("inf"):
            txt = "never (positive up to 64x)"
        else:
            txt = "already <= 0 at 1x" if be == 0 else f"{be:.1f}x"
        lines += ["## How wrong can the cost estimate be?",
                  f"Selected book net Sharpe at modelled costs: {breakeven['sharpe_x1']:.2f}. Net Sharpe turns "
                  f"non-positive at {txt} the modelled costs.", ""]
    lines += ["## Cost of a round trip by order size (includes fixed fees and commission)",
              "| order USD | " + " | ".join(sorted(costs.per_side_bps)) + " |",
              "|---|" + "---|" * len(costs.per_side_bps)]
    for size in (25, 100, 500, 2500):
        row = [f"{order_size_table(costs, c, (size,))[0]['round_trip_bps']:.1f}" for c in sorted(costs.per_side_bps)]
        lines.append(f"| {size} | " + " | ".join(row) + " |")
    lines += ["", "(bps of notional; a gross edge per trade should be at least 2x these numbers)", "",
              "## WHAT COULD I BE WRONG ABOUT?",
              "- Slippage is measured against the session open print, not a true arrival price; auction effects are "
              "folded in.", "- Few fills mean wide intervals; the model moves toward the data only slowly (k=30).",
              "- Paper-account fills are often better than real ones; calibrate again once real fills exist."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
