"""Step 8 report: combined portfolio vs baselines, sleeve contributions, correlations, realised vs target vol."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from bot.backtest import metrics
from bot.backtest.baselines import run_baselines
from bot.backtest.engine import BacktestResult, rebalance_dates, run_backtest
from bot.backtest.report import BANNER, HEAD, _row, cost_header, table
from bot.config import Costs, Risk, Universe, load_strategy
from bot.portfolio.construct import book_rebalance, combine, sleeve_returns
from bot.strategies import make_registry, split_universe


def build_portfolio_multi(panel: dict, selected: dict[str, dict], universe: Universe, risk: Risk, costs: Costs,
                          exec_mode: str = "next_open", tag: str = "MEASURED",
                          multipliers: tuple[float, ...] = (1.0,)):
    """Build sleeves and the combined weights once, then backtest at each cost multiplier."""
    reg = make_registry(load_strategy())
    parts = split_universe(panel["close"], universe)
    sleeves = {n: reg[n].build(panel["close"][parts[reg[n].universe]], p, risk) for n, p in selected.items()}
    weights, alloc = combine(sleeves, panel["close"], universe, risk)
    reb = rebalance_dates(weights.index, book_rebalance(selected, reg))
    results = {m: run_backtest({**panel, "volume": None}, weights, costs=costs, risk=risk, universe=universe,
                               rebalance=reb, exec_mode=exec_mode, tag=tag, cost_multiplier=m)
               for m in multipliers}
    return results, weights, alloc, sleeves


def build_portfolio(panel: dict, selected: dict[str, dict], universe: Universe, risk: Risk, costs: Costs,
                    exec_mode: str = "next_open", tag: str = "MEASURED", cost_multiplier: float = 1.0):
    results, weights, alloc, sleeves = build_portfolio_multi(
        panel, selected, universe, risk, costs, exec_mode, tag, (cost_multiplier,))
    return results[cost_multiplier], weights, alloc, sleeves


def portfolio_md(panel: dict, res: BacktestResult, weights: pd.DataFrame, alloc: pd.DataFrame,
                 sleeves: dict, universe: Universe, risk: Risk, costs: Costs, exec_mode: str, tag: str,
                 oos_start: pd.Timestamp, path: Path) -> dict:
    cash = res.cash_ret
    oos = res.net.loc[oos_start:]
    base = run_baselines(panel, universe, risk, costs, tag=tag, exec_mode=exec_mode)
    vol = metrics.ann_vol(oos)
    ratio = vol / risk.target_vol_annual
    max_gross = float(res.weights.sum(axis=1).max())
    rows = [_row("PORTFOLIO", oos, cash)] + [_row(n, b.net.loc[oos_start:], cash) for n, b in base.items()]
    contrib = {}
    for n, w in sleeves.items():
        sr = sleeve_returns(w.reindex(columns=panel["close"].columns).fillna(0.0), panel["close"])
        a = alloc[n].shift(1).fillna(0.0) if n in alloc else 0.0
        contrib[n] = float((sr * a).loc[oos_start:].sum())
    cols = panel["close"].columns
    corr = pd.DataFrame({n: sleeve_returns(w.reindex(columns=cols).fillna(0.0), panel["close"])
                         for n, w in sleeves.items()}).loc[oos_start:].corr()
    cs = metrics.cost_summary(res)
    lines = [f"# Portfolio v1 ({tag})", "", BANNER.get(tag, ""), cost_header(costs, exec_mode),
             f"Out-of-sample window starts {oos_start.date()} (earlier data was used only to warm up and select).", "",
             "## Performance vs baselines", table(rows, HEAD),
             "## Risk targeting",
             f"- Target vol {risk.target_vol_annual:.0%}; realised {vol:.1%} ({ratio:.0%} of target). "
             f"Acceptance (+/-25%): {'PASS' if abs(ratio - 1) <= 0.25 else 'FAIL'}. A shortfall is expected when "
             f"the 1.0 gross cap binds (trend off or low-vol periods).",
             f"- Maximum gross exposure {max_gross:.3f} (cap {risk.gross_exposure_max}): "
             f"{'PASS' if max_gross <= risk.gross_exposure_max + 1e-9 else 'FAIL'}",
             f"- Cost drag {cs['cost_drag_per_year']:.2%}/yr; turnover {cs['turnover_per_year']:.1f}/yr; "
             f"cost cover {cs['cost_cover_ratio']:.1f}x", "",
             "## Sleeve contribution (sum of allocated sleeve gross returns, OOS)",
             table([[n, f"{v:.1%}"] for n, v in contrib.items()], ["sleeve", "contribution"]),
             "## Sleeve correlation (OOS daily gross returns)", "```", corr.round(2).to_string(), "```", "",
             "## WHAT COULD I BE WRONG ABOUT?",
             "- Sleeve weights and the covariance are estimated from short windows and can lag regime changes.",
             "- The ladder's re-risk and re-arm rules are assumptions; results depend on them.",
             "- Costs are estimates; the 1.0 gross cap makes the book under-use its risk budget in calm trends."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"vol": vol, "vol_ratio": ratio, "max_gross": max_gross,
            "vol_ok": bool(abs(ratio - 1) <= 0.25), "gross_ok": bool(max_gross <= risk.gross_exposure_max + 1e-9),
            "sharpe": metrics.sharpe(oos, cash), "contrib": contrib, "isfinite": bool(np.isfinite(vol))}
