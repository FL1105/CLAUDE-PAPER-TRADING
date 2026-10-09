"""Strategy registry. Each strategy is a pure function: past prices -> target weights."""
from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from bot.config import Risk, Strategy, Universe
from bot.strategies.crypto_trend import crypto_trend_weights
from bot.strategies.mean_reversion import mean_reversion_weights
from bot.strategies.momentum_rotation import momentum_weights
from bot.strategies.trend import trend_weights


@dataclass(frozen=True)
class Spec:
    name: str
    grid: dict[str, list]
    rebalance: str
    universe: str  # "core" | "crypto"
    build: Callable[[pd.DataFrame, dict, Risk], pd.DataFrame]

    def combos(self) -> list[dict]:
        keys = list(self.grid)
        return [dict(zip(keys, v, strict=True)) for v in itertools.product(*self.grid.values())]


def make_registry(cfg: Strategy) -> dict[str, Spec]:
    t, m, r, c = cfg.trend, cfg.momentum_rotation, cfg.mean_reversion, cfg.crypto_trend
    return {
        "trend": Spec(
            "trend", {"sma": t["sma"], "vol_window": t["vol_window"]}, t["rebalance"], "core",
            lambda close, p, risk: trend_weights(
                close, p["sma"], p["vol_window"], risk.target_vol_annual, risk.max_weight_per_asset),
        ),
        "momentum_rotation": Spec(
            "momentum_rotation",
            {"lookback_months": m["lookback_months"], "top_n": m["top_n"]}, m["rebalance"], "core",
            lambda close, p, risk: momentum_weights(
                close, p["lookback_months"], p["top_n"], target_vol=risk.target_vol_annual,
                max_w=risk.max_weight_per_asset),
        ),
        "mean_reversion": Spec(
            "mean_reversion",
            {"rsi_entry": r["rsi_entry"], "rsi_exit": r["rsi_exit"],
             "max_hold_days": r["max_hold_days"]}, "daily", "core",
            lambda close, p, risk: mean_reversion_weights(
                close, p["rsi_entry"], p["rsi_exit"], p["max_hold_days"]),
        ),
        "crypto_trend": Spec(
            "crypto_trend", {"sma": c["sma"], "vol_cap": c["vol_cap"]}, c["rebalance"], "crypto",
            lambda close, p, risk: crypto_trend_weights(
                close, p["sma"], p["vol_cap"], sleeve_max=risk.crypto_sleeve_max),
        ),
    }


def split_universe(close: pd.DataFrame, universe: Universe) -> dict[str, list[str]]:
    core = [s for s in close.columns if universe.cls_of(s) != "crypto"]
    cry = [s for s in close.columns if universe.cls_of(s) == "crypto"]
    return {"core": core, "crypto": cry}
