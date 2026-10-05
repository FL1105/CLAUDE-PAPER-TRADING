"""Replay out-of-sample signals through the PaperExecutor with delayed prices.

Turns "the selected wallets made X% at their own prices" into "a copier would
have made Y% after latency, spread, depth and fees".

Assumptions:
    * Signals: BUY trades by each epoch's frozen wallet set, in time order. The
      selection (Phase 3 walk-forward) is leakage-safe; this module only scores.
    * Fill reference at delay d comes from ``price_at(trade, d)``. Synthetic worlds
      use drift toward the true probability; real-data backtests use the tape (last
      real trade in the token after the signal). A synthetic book with the
      configured half-spread/depth is laid on top of that reference.
    * Positions settle in time order as their markets resolve (freeing capacity
      exactly as in real time). Outcomes only SCORE fills, never choose them.
    * fill_rate = filled / signals that reached the book; signals skipped by risk
      limits (capacity) are reported separately as ``capacity_skipped``.
    * Shadow fills at 1x/2x/5x L give the latency decay curve (gate 7).

Failure mode guarded against: "unfillable signal" backtests that fill at the
whale's price with liquidity that was not there.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from polybot.config import Config
from polybot.data.schemas import BookSnapshot
from polybot.data.store import Store
from polybot.execution.latency import drift_price, synthetic_asks
from polybot.execution.paper import EvidenceMode, PaperExecutor, Signal

PriceAt = Callable[[Mapping[str, Any], float], float | None]


def token_id(market: object, outcome: object) -> str:
    return f"{market}:{int(outcome)}"  # type: ignore[call-overload]


def drift_price_fn(markets: pd.DataFrame, half_life_s: float) -> PriceAt:
    """Synthetic worlds: the price drifts from the wallet's price toward the true q."""
    q = markets.set_index("market")["q"]

    def f(tr: Mapping[str, Any], delay_s: float) -> float | None:
        qm = float(q.loc[tr["market"]])
        q_tok = qm if int(tr["outcome"]) == 0 else 1.0 - qm
        return drift_price(float(tr["price"]), q_tok, delay_s, half_life_s)

    return f


def tape_price_fn(trades: pd.DataFrame) -> PriceAt:
    """Real-data backtests: last real trade price in the same token in [t, t + delay]."""
    groups = {
        k: (g["ts"].to_numpy(dtype=np.int64), g["price"].to_numpy(dtype=np.float64))
        for k, g in trades.sort_values("ts", kind="mergesort").groupby(["market", "outcome"])
    }

    def f(tr: Mapping[str, Any], delay_s: float) -> float | None:
        ts, px = groups[(tr["market"], tr["outcome"])]
        t0 = int(tr["ts"])
        hi = int(np.searchsorted(ts, t0 + delay_s, side="right"))
        lo = int(np.searchsorted(ts, t0, side="left"))
        return float(px[hi - 1]) if hi > lo else None

    return f


@dataclass
class ReplayResult:
    fills: pd.DataFrame
    signals: pd.DataFrame
    settlements: pd.DataFrame
    summary: dict[str, Any]


def _book(
    token: str, ts: int, ref: float | None, cfg: Config, spread_mult: float
) -> BookSnapshot | None:
    if ref is None or not 0.0 < ref < 1.0:
        return None
    ex = cfg.execution
    asks = synthetic_asks(
        ref,
        half_spread=ex.sim_half_spread * spread_mult,
        levels=ex.sim_levels,
        depth_usdc=ex.sim_level_depth_usdc,
        tick=ex.default_tick_size,
    )
    return BookSnapshot(
        token_id=token,
        ts=ts,
        bids=(),
        asks=tuple(asks),
        tick_size=ex.default_tick_size,
        min_order_size=None,
    )


def replay(
    trades: pd.DataFrame,
    markets: pd.DataFrame,
    frozen_by_epoch: Sequence[tuple[int, int, tuple[str, ...]]],
    cfg: Config,
    price_at: PriceAt,
    *,
    evidence_mode: EvidenceMode,
    config_hash: str,
    fee_multiplier: float = 1.0,
    spread_multiplier: float = 1.0,
    latency_multiplier: float = 1.0,
) -> ReplayResult:
    """Replay (start_ts, end_ts, wallets) epochs. ``latency_multiplier`` scales L for
    the primary fill (stress tests); shadows always use 1x/2x/5x of the base L."""
    store = Store(":memory:")
    now = {"t": 0.0}
    ex = PaperExecutor(
        store, cfg, evidence_mode=evidence_mode, config_hash=config_hash, clock=lambda: now["t"]
    )
    L = cfg.execution.latency_L_s  # noqa: N806
    f = cfg.filters
    market_key: dict[str, Any] = {}  # condition_id string -> original market id
    mk = markets.set_index("market")
    win = mk["winning_outcome"]
    res_ts = mk["resolved_ts"] if "resolved_ts" in mk else mk["end_ts"]

    def settle_due(upto: float) -> None:
        """Settle every open market whose resolution is known by ``upto`` (in time order)."""
        due = []
        for cid in {p.condition_id for p in ex.state.open.values()}:
            key = market_key[cid]
            r, w = res_ts.get(key), win.get(key)
            if r is not None and not pd.isna(r) and r <= upto and w is not None and not pd.isna(w):
                due.append((int(r), cid, int(w)))
        for r, cid, w in sorted(due):
            toks = {
                p.token_id: int(p.token_id.rsplit(":", 1)[1])
                for p in ex.state.open.values()
                if p.condition_id == cid
            }
            now["t"] = float(r)
            ex.settle_market(
                cid, {t: 1.0 if i == w else 0.0 for t, i in toks.items()}, "resolved", r
            )

    for start, end, wallets in frozen_by_epoch:
        if not wallets:
            continue
        sel = trades.loc[
            (trades["ts"] >= start)
            & (trades["ts"] < end)
            & trades["wallet"].isin(wallets)
            & (trades["side"] == "BUY")
        ]
        sel = sel.sort_values(["ts", "wallet", "market"], kind="mergesort")
        rows: list[dict[Any, Any]] = sel.to_dict("records")
        for tr in rows:
            if not f.min_entry_price <= float(tr["price"]) <= f.max_entry_price:
                continue
            tok = token_id(tr["market"], tr["outcome"])
            market_key[str(tr["market"])] = tr["market"]
            t0 = int(tr["ts"])
            d = L * latency_multiplier
            settle_due(t0 + d)  # frees capacity exactly as it would have in real time
            now["t"] = t0 + d
            key = str(tr.get("tx_hash") or f"{tr['wallet']}|{tok}|{t0}|{tr['shares']}")
            s = Signal(
                strategy="wallet_copy",
                condition_id=str(tr["market"]),
                token_id=tok,
                ts=t0,
                wallet=str(tr["wallet"]),
                wallet_price=float(tr["price"]),
                wallet_trade_key=key,
            )
            book = _book(tok, int(t0 + d), price_at(tr, d), cfg, spread_multiplier)
            shadows = {
                m: _book(tok, int(t0 + m * L), price_at(tr, m * L), cfg, spread_multiplier)
                for m in cfg.execution.shadow_multipliers
            }
            ex.handle(s, book, shadows, fee_multiplier=fee_multiplier)

    settle_due(float("inf"))  # everything with a known outcome; the rest stays open
    fills, sigs, sets = store.fills_frame(), store.signals_frame(), store.settlements_frame()
    summary = summarize(fills, sigs, sets, markets)
    return ReplayResult(fills, sigs, sets, summary)


def summarize(
    fills: pd.DataFrame, sigs: pd.DataFrame, sets: pd.DataFrame, markets: pd.DataFrame
) -> dict[str, Any]:
    entries = sigs.loc[sigs["status"].isin(["filled", "skipped"])]
    skips = Counter(sigs.loc[sigs["status"] == "skipped", "skip_reason"].astype(str))
    risk = entries["skip_reason"].fillna("").str.startswith("risk:")
    reached_book = entries.loc[~risk]  # capacity skips are a sizing choice, not unfillable
    out: dict[str, Any] = {
        "signals": len(entries),
        "filled": int((sigs["status"] == "filled").sum()),
        "capacity_skipped": int(risk.sum()),
        "fill_rate": (
            float((reached_book["status"] == "filled").mean()) if len(reached_book) else None
        ),
        "skip_reasons": dict(sorted(skips.items())),
        "settled": len(sets),
    }
    if len(fills) == 0 or len(sets) == 0:
        out.update({"roi_net": None, "roi_by_latency": {}, "median_slippage": None})
        return out
    m = fills.merge(sets[["fill_id", "payout", "net_pnl"]], on="fill_id")
    out["roi_net"] = float(m["net_pnl"].sum() / m["stake_usdc"].sum())
    out["roi_gross_at_wallet_price"] = float(
        (
            (m["payout"] / m["shares"]) * (m["stake_usdc"] / m["wallet_price"]) - m["stake_usdc"]
        ).sum()
        / m["stake_usdc"].sum()
    )
    out["fees_paid"] = float(m["fee"].sum())
    out["median_slippage"] = float(fills["slippage"].median())
    out["mean_slippage_vs_best"] = float(fills["slippage_vs_best"].mean())
    payout_ps = m["payout"] / m["shares"]
    by_lat: dict[str, float | None] = {}
    for k in sorted({k for js in m["shadow_fills_json"] for k in _load(js)}, key=int):
        pnl = cost = 0.0
        for js, pps in zip(m["shadow_fills_json"], payout_ps, strict=True):
            sh = _load(js).get(k, {})
            if sh.get("status") in ("filled", "partial") and sh.get("shares"):
                pnl += float(sh["shares"]) * float(pps) - float(sh["cost"]) - float(sh["fee"])
                cost += float(sh["cost"])
        by_lat[f"{k}xL"] = pnl / cost if cost > 0 else None
    out["roi_by_latency"] = by_lat
    return out


def _load(js: str) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(js)
    return d
