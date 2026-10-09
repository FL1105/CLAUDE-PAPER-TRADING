"""Performance metrics. Sharpe/Sortino use returns in excess of the cash proxy unless noted."""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.backtest.engine import BacktestResult

ANN = 252


def cagr(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 2:
        return float("nan")
    g = float((1 + r).prod())
    return g ** (ANN / len(r)) - 1 if g > 0 else -1.0


def ann_vol(r: pd.Series) -> float:
    return float(r.std(ddof=1) * np.sqrt(ANN))


def sharpe(r: pd.Series, cash: pd.Series | float = 0.0) -> float:
    ex = r - (cash.reindex(r.index).fillna(0.0) if isinstance(cash, pd.Series) else cash)
    sd = ex.std(ddof=1)
    return float(ex.mean() / sd * np.sqrt(ANN)) if sd > 0 else 0.0


def sortino(r: pd.Series, cash: pd.Series | float = 0.0) -> float:
    ex = r - (cash.reindex(r.index).fillna(0.0) if isinstance(cash, pd.Series) else cash)
    dd = np.sqrt((np.minimum(ex, 0.0) ** 2).mean())
    return float(ex.mean() / dd * np.sqrt(ANN)) if dd > 0 else 0.0


def drawdown_series(r: pd.Series) -> pd.Series:
    eq = (1 + r).cumprod()
    peak = np.maximum(eq.cummax(), 1.0)  # the starting equity of 1.0 is a peak too
    return eq / peak - 1


def max_drawdown(r: pd.Series) -> float:
    return float(-drawdown_series(r).min())


def max_dd_duration(r: pd.Series) -> int:
    dd = drawdown_series(r) < 0
    run = dd.astype(int).groupby((~dd).cumsum()).cumsum()
    return int(run.max())


def ulcer_index(r: pd.Series) -> float:
    return float(np.sqrt((drawdown_series(r) ** 2).mean()))


def period_returns(r: pd.Series, freq: str) -> pd.Series:
    return (1 + r).groupby(r.index.to_period(freq)).prod() - 1


def summary(r: pd.Series, cash: pd.Series | None = None) -> dict:
    cash = cash if cash is not None else 0.0
    m = period_returns(r, "M")
    y = period_returns(r, "Y")
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    c = cagr(r)
    mdd = max_drawdown(r)
    return {
        "cagr": c, "vol": ann_vol(r), "sharpe": sharpe(r, cash), "sortino": sortino(r, cash),
        "max_dd": mdd, "max_dd_days": max_dd_duration(r), "calmar": c / mdd if mdd > 0 else float("nan"),
        "ulcer": ulcer_index(r), "worst_day": float(r.min()), "worst_month": float(m.min()),
        "worst_year": float(y.min()), "hit_rate": float((r > 0).mean()),
        "profit_factor": float(pos / neg) if neg > 0 else float("inf"),
        "expectancy_daily_bps": float(r.mean() * 1e4), "n_days": int(len(r)),
    }


def cost_summary(res: BacktestResult) -> dict:
    yrs = len(res.net) / ANN
    gross_sum, cost_sum = float(res.gross.sum()), float(res.cost.sum())
    n_ord = int(res.orders.sum())
    return {
        "turnover_per_year": float(res.turnover.sum() / yrs),
        "cost_drag_per_year": cost_sum / yrs,
        "orders_per_year": n_ord / yrs,
        "avg_net_bps_per_order": float(res.net.sum() / n_ord * 1e4) if n_ord else float("nan"),
        "cost_cover_ratio": gross_sum / cost_sum if cost_sum > 0 else float("inf"),
        "gross_cagr": cagr(res.gross), "net_cagr": cagr(res.net),
    }
