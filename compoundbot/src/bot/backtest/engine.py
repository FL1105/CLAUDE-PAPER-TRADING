"""Daily backtest engine.

Timing (no lookahead): targets[t] are computed from data through the close of day t.
They are executed on day t+1:
  exec_mode="next_open"  : old weights earn the overnight gap, trade at the open,
                           new weights earn the session (open -> close).
  exec_mode="next_close" : old weights earn the full day, trade at the close.
Cash (unallocated weight) earns the cash-proxy return. Costs come from costs.py.
The drawdown ladder (risk.drawdown) scales targets and is part of every reported number.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot.backtest.costs import side_bps_vector, trade_cost_fraction
from bot.config import Costs, Risk, Universe, load_costs, load_risk, load_universe
from bot.risk.drawdown import Ladder


@dataclass
class BacktestResult:
    net: pd.Series
    gross: pd.Series
    cost: pd.Series
    turnover: pd.Series
    orders: pd.Series
    weights: pd.DataFrame
    mult: pd.Series
    cash_ret: pd.Series
    tag: str = "MEASURED"
    meta: dict = field(default_factory=dict)

    @property
    def equity(self) -> pd.Series:
        return (1 + self.net).cumprod()


def rebalance_dates(index: pd.DatetimeIndex, freq: str) -> pd.Series:
    """Boolean mask: True on the last session of each week/month ('daily' = every day)."""
    s = pd.Series(index, index=index)
    if freq == "daily":
        return pd.Series(True, index=index)
    key = s.dt.to_period("W-FRI" if freq == "weekly" else "M")
    last = s.groupby(key.values).transform("max")
    return pd.Series((s == last).to_numpy(), index=index)


def run_backtest(
    panel: dict[str, pd.DataFrame],
    targets: pd.DataFrame,
    *,
    costs: Costs | None = None,
    risk: Risk | None = None,
    universe: Universe | None = None,
    rebalance: pd.Series | None = None,
    exec_mode: str = "next_open",
    cost_multiplier: float = 1.0,
    use_ladder: bool = True,
    capital: float | None = None,
    tag: str = "MEASURED",
    start: pd.Timestamp | None = None,
) -> BacktestResult:
    costs = costs or load_costs()
    risk = risk or load_risk()
    universe = universe or load_universe()
    capital = capital or costs.start_capital_usd
    close, open_ = panel["close"], panel["open"]
    cash = panel["cash"].iloc[:, 0]
    idx = close.index
    syms = list(targets.columns)
    close, open_ = close[syms], open_[syms]
    classes = [universe.cls_of(s) for s in syms]
    side_rate = side_bps_vector(costs, classes, cost_multiplier)

    n, k = len(idx), len(syms)
    tgt = targets.reindex(idx).fillna(0.0).to_numpy(float)
    px_c = close.to_numpy(float)
    px_o = open_.to_numpy(float)
    avail = ~np.isnan(px_c)
    cash_r = cash.pct_change().fillna(0.0).to_numpy(float)
    reb = (rebalance.reindex(idx).fillna(False).to_numpy(bool) if rebalance is not None
           else np.ones(n, bool))
    start_i = 1 if start is None else max(1, idx.searchsorted(start))

    cur = np.zeros(k)
    equity = 1.0
    ladder = Ladder(risk, auto_rearm=True)
    prev_mult = 1.0
    net = np.zeros(n)
    gross = np.zeros(n)
    cost_s = np.zeros(n)
    turn = np.zeros(n)
    orders_s = np.zeros(n)
    wts = np.zeros((n, k))
    mults = np.ones(n)
    last_ret = 0.0

    for d in range(start_i, n):
        t = d - 1
        majority = bool((tgt[t] > 1e-9).sum() >= max(1, 0.5 * avail[t].sum()))
        mult = ladder.update(equity, majority) if use_ladder else 1.0
        mults[d] = mult
        desired = tgt[t] * mult * avail[d]
        desired = np.where(avail[d], desired, 0.0)
        if last_ret <= -risk.daily_loss_halt:
            desired = np.minimum(desired, cur)  # no risk increase after a daily-loss halt
        force = mult != prev_mult
        prev_mult = mult
        do_reb = reb[t] or force

        r_c = np.where(avail[d] & avail[t], px_c[d] / np.where(avail[t], px_c[t], 1) - 1, 0.0)
        r_c = np.nan_to_num(r_c)
        cr = cash_r[d]

        if exec_mode == "next_open":
            g1 = np.nan_to_num(np.where(avail[d] & avail[t], px_o[d] / np.where(avail[t], px_c[t], 1) - 1, 0.0))
            g2 = np.nan_to_num(np.where(avail[d], px_c[d] / np.where(avail[d], px_o[d], 1) - 1, 0.0))
            eo = 1.0 + float((cur * g1).sum())
            drift = cur * (1 + g1) / eo
            new, delta, cfrac, n_ord = _trade(drift, desired, do_reb, risk, costs, side_rate,
                                              eo * equity * capital)
            w_after = _after_cost(new, cfrac)
            g_net = 1 + float((w_after * g2).sum()) + (1 - w_after.sum()) * cr
            g_gross = 1 + float((new * g2).sum()) + (1 - new.sum()) * cr
            ret_g = eo * g_gross - 1
            ret_n = eo * (1 - cfrac) * g_net - 1
            cur = w_after * (1 + g2) / g_net
        else:
            ret_g = float((cur * r_c).sum()) + (1 - cur.sum()) * cr
            e_c = 1.0 + ret_g
            drift = cur * (1 + r_c) / e_c
            new, delta, cfrac, n_ord = _trade(drift, desired, do_reb, risk, costs, side_rate,
                                              e_c * equity * capital)
            ret_n = e_c * (1 - cfrac) - 1
            cur = _after_cost(new, cfrac)
        gross[d], net[d], cost_s[d] = ret_g, ret_n, ret_g - ret_n
        turn[d], orders_s[d] = float(np.abs(delta).sum()), n_ord
        wts[d] = cur
        equity *= 1 + ret_n
        last_ret = ret_n
    sl = slice(start_i, None)
    ix = idx[sl]
    out = BacktestResult(
        net=pd.Series(net[sl], ix), gross=pd.Series(gross[sl], ix), cost=pd.Series(cost_s[sl], ix),
        turnover=pd.Series(turn[sl], ix), orders=pd.Series(orders_s[sl], ix),
        weights=pd.DataFrame(wts[sl], ix, syms), mult=pd.Series(mults[sl], ix),
        cash_ret=pd.Series(cash_r[sl], ix), tag=tag,
        meta={"exec_mode": exec_mode, "cost_multiplier": cost_multiplier, "ladder": use_ladder},
    )
    return out


def _trade(drift, desired, do_reb, risk: Risk, costs: Costs, side_rate, equity_usd, *_unused):
    """Apply no-trade band and minimum order size; return new weights, deltas, cost frac, orders."""
    if not do_reb:
        return drift.copy(), np.zeros_like(drift), 0.0, 0
    delta = desired - drift
    exit_full = (desired == 0) & (drift > 1e-9)
    skip = (np.abs(delta) <= risk.no_trade_band) & ~exit_full
    skip |= (np.abs(delta) * equity_usd < costs.min_order_usd) & ~exit_full
    delta = np.where(skip, 0.0, delta)
    new = drift + delta
    # cannot spend more than 100%
    gross = new.sum()
    if gross > risk.gross_exposure_max + 1e-9:
        new = new * (risk.gross_exposure_max / gross)
        delta = new - drift
    cfrac, n_ord = trade_cost_fraction(delta, side_rate, max(equity_usd, 1e-9), costs)
    return new, delta, cfrac, n_ord


def _after_cost(new: np.ndarray, cfrac: float) -> np.ndarray:
    """Weights relative to equity after paying `cfrac`: from cash first, then pro-rata."""
    invested = float(new.sum())
    if cfrac <= 0 or invested <= 0:
        return new
    if 1.0 - invested >= cfrac:
        return new / (1.0 - cfrac)
    return new / invested
