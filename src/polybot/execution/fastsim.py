"""In-memory copy engine for placebo draws and stress runs: same math, no ledger.

The placebo needs about 200 copy-engine runs per evaluation, which is too slow
through SQLite. This module re-implements ``replay`` + ``PaperExecutor`` logic in
memory (risk limits, chase rule, synthetic book, book walk, fees, time-ordered
settlement), reusing the SAME ``walk_book``, ``depth_within``, ``fee_coeff``,
``fill_fee`` and ``synthetic_asks`` functions.

Assumption / guard: "identical copy engine" is enforced by a test that requires
``fast_copy`` to match ``replay`` exactly (fills, skips, net ROI) on synthetic
worlds. If the executor changes and this module does not, that test fails.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from polybot.config import Config
from polybot.execution.book import depth_within, walk_book
from polybot.execution.fees import fee_coeff, fill_fee
from polybot.execution.latency import synthetic_asks
from polybot.execution.replay import PriceAt, prepare_signals, signal_key

__all__ = ["FastResult", "fast_copy", "prepare_signals"]


@dataclass(frozen=True)
class FastResult:
    signals: int
    filled: int
    capacity_skipped: int
    not_reached: int  # capacity skips + signals with no reference price
    fill_rate: float | None
    settled: int
    pnl: float
    cost: float

    @property
    def roi(self) -> float | None:
        return self.pnl / self.cost if self.cost > 0 else None


def fast_copy(
    rows: Sequence[dict[Any, Any]],
    markets: pd.DataFrame,
    cfg: Config,
    price_at: PriceAt,
    *,
    fee_multiplier: float = 1.0,
    spread_multiplier: float = 1.0,
    latency_multiplier: float = 1.0,
) -> FastResult:
    ex, pc = cfg.execution, cfg.paper
    mk = markets.set_index("market")
    win = mk["winning_outcome"].to_dict()
    res = (mk["resolved_ts"] if "resolved_ts" in mk else mk["end_ts"]).to_dict()
    b0 = pc.bankroll_usdc
    stake = pc.stake_frac * b0
    d = ex.latency_L_s * latency_multiplier
    coeff = fee_coeff(cfg.fees, None, fee_multiplier)
    tick = ex.default_tick_size

    cash = b0
    open_pos: dict[
        Any, list[tuple[int, float, float, float]]
    ] = {}  # market -> (out, sh, cost, fee)
    exposure = 0.0
    n_open = 0
    heap: list[tuple[int, str, Any]] = []
    seen: set[str] = set()
    signals = filled = capacity = settled = no_ref = 0
    pnl = cost_settled = 0.0

    def settle_due(upto: float) -> None:
        nonlocal cash, exposure, n_open, settled, pnl, cost_settled
        while heap and heap[0][0] <= upto:
            _, _, m = heapq.heappop(heap)
            w = int(win[m])
            for out, sh, c, fe in open_pos.pop(m, []):
                payout = sh * (1.0 if out == w else 0.0)
                cash += payout
                exposure -= c
                n_open -= 1
                settled += 1
                pnl += payout - c - fe
                cost_settled += c

    for tr in rows:
        t0 = int(tr["ts"])
        settle_due(t0 + d)
        key = signal_key(tr)
        if key in seen:
            continue  # duplicate: not counted, exactly like the executor
        seen.add(key)
        signals += 1
        ref = price_at(tr, d)
        if ref is None or not 0.0 < ref < 1.0:
            no_ref += 1  # same order as replay: checked before the risk limits
            continue
        m = tr["market"]
        mexp = sum(c for _, _, c, _ in open_pos.get(m, []))
        if (
            n_open >= pc.max_concurrent_positions
            or mexp + stake > pc.max_market_frac * b0 + 1e-9
            or exposure + stake > pc.max_total_exposure_frac * b0 + 1e-9
            or cash < stake * 1.1
        ):
            capacity += 1
            continue
        asks = synthetic_asks(
            ref,
            half_spread=ex.sim_half_spread * spread_multiplier,
            levels=ex.sim_levels,
            depth_usdc=ex.sim_level_depth_usdc,
            tick=tick,
        )
        if not asks:
            continue
        wp = float(tr["price"])
        best = asks[0].price
        max_price = wp + ex.max_chase
        if best > max_price + 1e-9 or depth_within(asks, best, ex.max_chase) < stake - 1e-9:
            continue
        fr = walk_book(
            asks,
            stake,
            tick=tick,
            insufficient=ex.insufficient_depth,
            max_price=max_price,
            share_decimals=ex.share_decimals,
        )
        if not fr.filled:
            continue
        fee = fill_fee(fr.levels, coeff)
        cash -= fr.cost + fee
        exposure += fr.cost
        n_open += 1
        filled += 1
        if m not in open_pos:
            open_pos[m] = []
            r, w = res.get(m), win.get(m)
            if r is not None and not pd.isna(r) and w is not None and not pd.isna(w):
                heapq.heappush(heap, (int(r), str(m), m))
        open_pos[m].append((int(tr["outcome"]), fr.shares, fr.cost, fee))
    settle_due(float("inf"))
    reached = signals - capacity - no_ref
    return FastResult(
        signals,
        filled,
        capacity,
        capacity + no_ref,
        filled / reached if reached else None,
        settled,
        pnl,
        cost_settled,
    )
