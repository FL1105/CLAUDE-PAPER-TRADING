"""Baseline weight generators: buy-and-hold SPY, 60/40, equal-weight, cash and vol-targeted versions."""
from __future__ import annotations

import pandas as pd

from bot.backtest.engine import BacktestResult, rebalance_dates, run_backtest
from bot.config import Costs, Risk, Universe
from bot.portfolio.vol_target import target_vol_book

NAMES = ["spy", "sixty_forty", "equal_weight", "cash", "vt_equal_weight", "vt_sixty_forty"]


def baseline_weights(close: pd.DataFrame, name: str, universe: Universe, risk: Risk) -> pd.DataFrame:
    z = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    core = [s for s in close.columns if universe.cls_of(s) != "crypto"]
    if name == "cash":
        return z
    if name == "spy":
        z["SPY"] = 1.0
        return z
    if name in ("sixty_forty", "vt_sixty_forty"):
        z["SPY"], z["IEF"] = 0.6, 0.4
    else:  # equal weight over assets that exist at each date
        avail = close[core].notna().astype(float)
        z[core] = avail.div(avail.sum(axis=1).where(avail.sum(axis=1) > 0), axis=0).fillna(0.0)
    if name.startswith("vt_"):
        z = target_vol_book(z, close, risk.target_vol_annual, risk.vol_window,
                            max(risk.max_weight_per_asset, 0.6), risk.gross_exposure_max)
    return z


def run_baselines(panel: dict, universe: Universe, risk: Risk, costs: Costs,
                  tag: str = "MEASURED", exec_mode: str = "next_open",
                  names: list[str] | None = None) -> dict[str, BacktestResult]:
    close = panel["close"]
    out = {}
    for n in names or NAMES:
        w = baseline_weights(close, n, universe, risk)
        # baselines are passive: no drawdown ladder, rebalanced monthly
        out[n] = run_backtest(
            panel, w, costs=costs, risk=risk, universe=universe,
            rebalance=rebalance_dates(close.index, "monthly"), exec_mode=exec_mode,
            use_ladder=False, tag=tag)
    return out
