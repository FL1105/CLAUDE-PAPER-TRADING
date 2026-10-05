"""Leakage-safe walk-forward: epochs, frozen wallet sets, out-of-sample evaluation.

Assumptions:
    * Input frames: ``trades`` (wallet, market, outcome, side, price, shares, ts)
      and ``markets`` (market, status, winning_outcome, resolved_ts), with
      ``winning_outcome`` NaN unless the market resolved cleanly.
    * Time is cut into epochs of ``walkforward.epoch_days``. For epoch k the
      selector sees ONLY a :class:`DataView` cut at the epoch start.
    * OOS signals = BUY trades by the frozen set *during* the epoch. They are
      scored at the wallet's own price (gross); realistic fills and fees are
      added in Phase 4.

Failure modes guarded against:
    * Look-ahead / leakage: the selector never receives the full frames. A
      DataView holds copies with every trade at or after the cutoff removed, and
      every resolution not known before the cutoff erased. The window only uses
      markets that fully resolved before the epoch start. Trades in markets
      resolving later are purged and counted. Tests mutate the future and
      assert that the selection does not change.
    * Moving targets: the selected set is frozen at epoch start (a tuple).
    * Selection bias going unnoticed: a naive top-N-by-PnL list is evaluated
      alongside for contrast.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from polybot.config import Config
from polybot.selection.filters import EligibilityResult, apply_filters
from polybot.stats.edge import build_units
from polybot.stats.fdr import benjamini_hochberg

DAY = 86_400


class DataView:
    """Everything knowable strictly before ``cutoff``, and nothing else.

    Construction copies and filters; the full frames are not retained, so no
    method can return post-cutoff data.
    """

    __slots__ = ("_cutoff", "_markets", "_trades")

    def __init__(self, trades: pd.DataFrame, markets: pd.DataFrame, cutoff: int) -> None:
        self._cutoff = int(cutoff)
        self._trades = trades.loc[trades["ts"] < self._cutoff].copy()
        m = markets.copy()
        known = m["resolved_ts"].notna() & (m["resolved_ts"] < self._cutoff)
        m.loc[~known, "winning_outcome"] = np.nan
        m.loc[~known, "resolved_ts"] = np.nan
        m.loc[~known, "status"] = "open"
        self._markets = m

    @property
    def cutoff(self) -> int:
        return self._cutoff

    def trades(self) -> pd.DataFrame:
        return self._trades.copy()

    def markets(self) -> pd.DataFrame:
        return self._markets.copy()


@dataclass(frozen=True)
class Epoch:
    index: int
    start_ts: int
    end_ts: int


def make_epochs(start_ts: int, end_ts: int, epoch_days: int) -> list[Epoch]:
    if end_ts <= start_ts:
        raise ValueError("end_ts must be after start_ts")
    step = epoch_days * DAY
    out = []
    s, i = start_ts, 0
    while s + step <= end_ts:
        out.append(Epoch(i, s, s + step))
        s += step
        i += 1
    return out


@dataclass
class Selection:
    epoch: Epoch
    wallets: tuple[str, ...]  # frozen BH-selected set
    naive: tuple[str, ...]  # naive top-N by in-sample PnL (contrast only)
    cap: float
    diagnostics: dict[str, Any] = field(default_factory=dict)
    edges: pd.DataFrame | None = None


Selector = Callable[[DataView, Epoch], Selection]


def select_wallets(view: DataView, epoch: Epoch, cfg: Config, seed: int) -> Selection:
    """Real selector: trailing window of markets resolved before the cutoff."""
    if view.cutoff != epoch.start_ts:
        raise ValueError("view must be cut at the epoch start")
    wf, st = cfg.walkforward, cfg.stats
    win_start = epoch.start_ts - wf.trailing_window_days * DAY
    m = view.markets()
    in_win = m.loc[m["resolved_ts"].notna() & (m["resolved_ts"] >= win_start)]
    t = view.trades()
    recent = t.loc[t["ts"] >= win_start]
    purged = int((~recent["market"].isin(in_win["market"])).sum())  # unresolved info
    sel_trades = t.loc[t["market"].isin(in_win["market"])]

    units = build_units(sel_trades, in_win[["market", "winning_outcome"]])
    elig: EligibilityResult = apply_filters(
        sel_trades,
        units,
        in_win.set_index("market")["resolved_ts"],
        cfg.filters,
        cap_quantile=st.stake_cap_quantile,
        small_n_exact_threshold=st.small_n_exact_threshold,
        seed=seed,
        n_mc=cfg.synthetic.calibration_mc_sims,
    )
    e = elig.edges
    cand = e.loc[e["wallet"].isin(elig.eligible)].reset_index(drop=True)
    bh = benjamini_hochberg(cand["p_value"].to_numpy(dtype=np.float64), st.fdr_q)
    chosen = tuple(sorted(cand.loc[bh.reject, "wallet"].astype(str)))
    naive = tuple(
        e.sort_values(["total_pnl", "wallet"], ascending=[False, True])
        .head(st.naive_top_n)["wallet"]
        .astype(str)
    )
    diag = {
        "window_start": win_start,
        "markets_in_window": len(in_win),
        "units": len(units.units),
        "two_sided_pairs": len(units.two_sided),
        "purged_trades_unresolved_markets": purged,
        "filters": elig.as_dict(),
        "bh_rejected": bh.n_rejected,
        "bh_threshold": bh.threshold,
    }
    return Selection(epoch, chosen, naive, elig.cap, diag, e)


def oos_stats(units: pd.DataFrame, wallets: tuple[str, ...], cap: float) -> dict[str, Any]:
    """OOS performance of a frozen set (gross, at the wallet's own prices)."""
    u = units.loc[units["wallet"].isin(wallets)]
    if u.empty or not np.isfinite(cap):
        return {
            "n_wallets": len(wallets),
            "n_units": 0,
            "roi": None,
            "pnl": 0.0,
            "cost": 0.0,
            "pooled_z": None,
            "wallet_z": {},
        }
    p = u["p_bar"].to_numpy(dtype=np.float64)
    y = u["y"].to_numpy(dtype=np.float64)
    w = np.minimum(u["cost"].to_numpy(dtype=np.float64), cap)
    df = pd.DataFrame(
        {"wallet": u["wallet"].to_numpy(), "num": w * (y - p), "den": w * w * p * (1 - p)}
    )
    g = df.groupby("wallet").sum()
    wz = (g["num"] / np.sqrt(g["den"].where(g["den"] > 0))).fillna(0.0)
    den = float(df["den"].sum())
    return {
        "n_wallets": len(wallets),
        "n_units": len(u),
        "roi": float(u["pnl"].sum() / u["cost"].sum()),
        "pnl": float(u["pnl"].sum()),
        "cost": float(u["cost"].sum()),
        "pooled_z": float(df["num"].sum() / np.sqrt(den)) if den > 0 else None,
        "wallet_z": {str(k): float(v) for k, v in wz.items()},
    }


@dataclass
class WalkForwardResult:
    epochs: list[dict[str, Any]]
    aggregate: dict[str, Any]


def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = sum(r["pnl"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    wz = [z for r in rows for z in r["wallet_z"].values()]
    return {
        "epochs_with_signals": sum(1 for r in rows if r["n_units"]),
        "n_units": sum(r["n_units"] for r in rows),
        "roi": pnl / cost if cost > 0 else None,
        "mean_wallet_oos_z": float(np.mean(wz)) if wz else None,
        "frac_wallets_positive_oos": float(np.mean([z > 0 for z in wz])) if wz else None,
        "n_wallet_epochs": len(wz),
    }


def walk_forward(
    trades: pd.DataFrame,
    markets: pd.DataFrame,
    cfg: Config,
    start_ts: int,
    end_ts: int,
    *,
    seed: int,
    selector: Selector | None = None,
) -> WalkForwardResult:
    """Run selection at each epoch start and evaluate the frozen set out of sample."""
    sel_fn: Selector = selector or (lambda v, ep: select_wallets(v, ep, cfg, seed + ep.index))
    final = markets[["market", "winning_outcome"]]
    epochs = make_epochs(start_ts, end_ts, cfg.walkforward.epoch_days)
    rows: list[dict[str, Any]] = []
    sel_rows: list[dict[str, Any]] = []
    naive_rows: list[dict[str, Any]] = []
    for ep in epochs:
        sel = sel_fn(DataView(trades, markets, ep.start_ts), ep)
        if sel.epoch != ep:
            raise ValueError("selector returned a selection for the wrong epoch")
        in_ep = trades.loc[(trades["ts"] >= ep.start_ts) & (trades["ts"] < ep.end_ts)]
        oos_units = build_units(in_ep, final).units  # scored after the fact, never fed back
        s = oos_stats(oos_units, sel.wallets, sel.cap)
        n = oos_stats(oos_units, sel.naive, sel.cap)
        sel_rows.append(s)
        naive_rows.append(n)
        pending = int(
            in_ep.loc[in_ep["wallet"].isin(sel.wallets), "market"]
            .isin(final.loc[final["winning_outcome"].isna(), "market"])
            .sum()
        )
        rows.append(
            {
                "epoch": ep.index,
                "start_ts": ep.start_ts,
                "end_ts": ep.end_ts,
                "selected": list(sel.wallets),
                "n_selected": len(sel.wallets),
                "oos_roi": s["roi"],
                "oos_pooled_z": s["pooled_z"],
                "oos_units": s["n_units"],
                "pending_unresolved_trades": pending,
                "naive_oos_roi": n["roi"],
                "naive_oos_pooled_z": n["pooled_z"],
                "diagnostics": sel.diagnostics,
            }
        )
    return WalkForwardResult(
        rows, {"selected": _agg(sel_rows), "naive": _agg(naive_rows), "n_epochs": len(epochs)}
    )


def make_leaky_selector(trades: pd.DataFrame, markets: pd.DataFrame, cfg: Config) -> Selector:
    """LEAKAGE CANARY. Ranks wallets by PnL inside the epoch being evaluated.

    Exists only so tests can show that leakage produces an obviously inflated OOS
    result and that the real selector cannot reproduce it. Never use it for decisions.
    """
    final = markets[["market", "winning_outcome"]]

    def leaky(view: DataView, ep: Epoch) -> Selection:
        fut = trades.loc[(trades["ts"] >= ep.start_ts) & (trades["ts"] < ep.end_ts)]
        u = build_units(fut, final).units
        if u.empty:
            return Selection(ep, (), (), float("inf"))
        pnl = u.groupby("wallet")["pnl"].sum().sort_values(ascending=False)
        top = tuple(sorted(pnl.head(cfg.stats.naive_top_n).index.astype(str)))
        cap = float(np.quantile(u["cost"], cfg.stats.stake_cap_quantile))
        return Selection(ep, top, top, cap, {"LEAKY": True})

    return leaky
