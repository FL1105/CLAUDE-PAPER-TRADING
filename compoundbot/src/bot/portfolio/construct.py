"""Combine KEPT strategy sleeves by inverse-volatility risk budgeting, then vol-target the book."""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.config import Risk, Universe
from bot.portfolio.vol_target import target_vol_book


def sleeve_returns(weights: pd.DataFrame, close: pd.DataFrame) -> pd.Series:
    """Gross daily return of a sleeve (weights decided at t earn day t+1). Costs ignored here:
    only used to size sleeve risk budgets."""
    r = close.pct_change(fill_method=None).fillna(0.0)
    return (weights.shift(1).fillna(0.0) * r.reindex(columns=weights.columns).fillna(0.0)).sum(axis=1)


def inverse_vol_sleeve_weights(sleeve_rets: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    """Causal sleeve weights summing to 1: a_s(t) ~ 1/vol_s(t) using returns through t."""
    vol = sleeve_rets.rolling(window, min_periods=window).std()
    iv = (1.0 / vol.where(vol > 1e-9)).fillna(0.0)
    tot = iv.sum(axis=1)
    return iv.div(tot.where(tot > 0), axis=0).fillna(0.0)


def combine(
    sleeves: dict[str, pd.DataFrame], close: pd.DataFrame, universe: Universe, risk: Risk,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (final target weights over close.columns, sleeve allocation weights)."""
    cols = list(close.columns)
    full = {k: w.reindex(index=close.index, columns=cols).fillna(0.0) for k, w in sleeves.items()}
    if not full:
        return pd.DataFrame(0.0, index=close.index, columns=cols), pd.DataFrame(index=close.index)
    rets = pd.DataFrame({k: sleeve_returns(w, close) for k, w in full.items()})
    # a sleeve with no risk yet (all flat) gets zero weight; others share by inverse vol
    alloc = inverse_vol_sleeve_weights(rets)
    combo = sum(full[k].mul(alloc[k], axis=0) for k in full)
    crypto_cols = [c for c in cols if universe.cls_of(c) == "crypto"]
    scaled = target_vol_book(
        combo, close, risk.target_vol_annual, risk.vol_window, risk.max_weight_per_asset,
        risk.gross_exposure_max)
    if crypto_cols:
        tot = scaled[crypto_cols].sum(axis=1)
        over = tot > risk.crypto_sleeve_max
        if over.any():
            scaled.loc[over, crypto_cols] = (
                scaled.loc[over, crypto_cols].div(tot[over], axis=0) * risk.crypto_sleeve_max)
    return scaled.replace([np.inf, -np.inf], 0.0), alloc


def book_rebalance(selected: dict, registry: dict) -> str:
    """Book-level schedule: daily if any selected sleeve trades daily (mean reversion), else weekly.
    Used identically by the backtest and the live runner so their trade days match."""
    return "daily" if any(registry[n].rebalance == "daily" for n in selected) else "weekly"
