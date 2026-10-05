"""Promotion ladder (rarity tiers), kill switches, micro-live (S3) evaluator.

Tiers:
    Common    S0  walk-forward backtest valid (OOS net edge > 0 across >= min_epochs).
                  A backtest alone can never promote past this tier.
    Uncommon  S1  live-forward paper running: gates 1 and 2 pass.
    Rare      S2  all 12 paper gates pass: eligible to CONSIDER micro-live, pending
                  the manual checklist.
    Epic      S3  micro-live validated (future task; evaluator only here).
    Legendary S4  scaled (future task).

Kill switches are evaluated continuously. Any trigger halts paper trading: the
executor refuses new signals until a human clears the halt.

Failure mode guarded against: a green-looking dashboard while the strategy is
silently breaking, and "almost passed" being treated as passed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.stats import norm

from polybot.config import Config
from polybot.gates import metrics as M  # noqa: N812
from polybot.gates.gates import Admissibility, GateResult, evaluate

TIERS = (
    ("Common", "S0 walk-forward backtest valid"),
    ("Uncommon", "S1 forward paper running"),
    ("Rare", "S2 paper gates passed"),
    ("Epic", "S3 micro-live passed"),
    ("Legendary", "S4 scaled"),
)

MANUAL_CHECKLIST = (
    "Confirm legal eligibility to trade event contracts in my state and on the specific "
    "platform I would use. Access for US residents runs through a regulated, KYC'd, "
    "intermediated platform, not a self-custody wallet, and its available markets can differ "
    "from the international platform whose on-chain wallet data we analyze.",
    "Confirm the platform's terms and API actually permit automated trading from my account "
    "type, and that the markets the strategy trades are available to me there.",
    "Confirm I understand tax reporting for gains and losses.",
    "Decide the micro-live bankroll as an amount I can lose entirely without it changing my "
    "life. The system never suggests this number.",
    "Credentials will be stored outside the repo with minimum permissions and a "
    "withdrawal-disabled key where the platform allows.",
)


@dataclass(frozen=True)
class LadderResult:
    tier: str | None  # None = not even Common
    stage: str | None
    gates: list[GateResult]
    blocking_next: list[GateResult]
    next_tier: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "stage": self.stage,
            "next_tier": self.next_tier,
            "blocking_next": [g.as_dict() for g in self.blocking_next],
            "gates": [g.as_dict() for g in self.gates],
            "manual_checklist": [{"item": c, "checked": False} for c in MANUAL_CHECKLIST],
        }


def s0_result(ev: M.Evidence, cfg: Config) -> GateResult:
    r = M.roi(ev.trades)
    ne = int(ev.trades["epoch"].nunique()) if len(ev.trades) else 0
    ok = r is not None and r > 0 and ne >= cfg.gates.min_epochs
    return GateResult(
        "S0_oos_net_edge",
        ok,
        {"roi": None if r is None else round(r, 4), "epochs": ne},
        {"roi": "> 0", "epochs": cfg.gates.min_epochs},
        "out-of-sample net edge across epochs",
    )


def ladder(ev: M.Evidence, cfg: Config, admissibility: Admissibility = "strict") -> LadderResult:
    gates = evaluate(ev, cfg, admissibility)
    s0 = s0_result(ev, cfg)
    s1_need = [g for g in gates if g.name.startswith(("1_", "2_"))]
    if not s0.passed:
        return LadderResult(None, None, gates, [s0], TIERS[0][0])
    if not all(g.passed for g in s1_need):
        return LadderResult(
            TIERS[0][0], TIERS[0][1], gates, [g for g in s1_need if not g.passed], TIERS[1][0]
        )
    if not all(g.passed for g in gates):
        return LadderResult(
            TIERS[1][0], TIERS[1][1], gates, [g for g in gates if not g.passed], TIERS[2][0]
        )
    blocker = GateResult(
        "S3_micro_live",
        False,
        None,
        None,
        "requires the manual checklist and a separate, future micro-live task",
    )
    return LadderResult(TIERS[2][0], TIERS[2][1], gates, [blocker], TIERS[3][0])


# --- kill switches ---------------------------------------------------------------------


@dataclass(frozen=True)
class KillSwitch:
    name: str
    triggered: bool
    detail: str


def kill_switches(
    ev: M.Evidence,
    cfg: Config,
    *,
    modeled_slippage: float | None = None,
    daily_median_slippage: list[float] | None = None,
) -> list[KillSwitch]:
    ks, t = cfg.kill_switches, ev.trades
    out = []
    dd = M.max_drawdown(t, ev.bankroll)
    out.append(
        KillSwitch(
            "drawdown",
            dd > ks.max_drawdown_frac,
            f"max settled drawdown {dd:.1%} vs {ks.max_drawdown_frac:.0%}",
        )
    )
    thr = M.rolling_roi_threshold(
        t, ks.rolling_window_trades, ks.rolling_roi_percentile, sims=4000, seed=cfg.seed
    )
    if thr is None:
        out.append(
            KillSwitch(
                "rolling_roi", False, f"fewer than {ks.rolling_window_trades} settled trades"
            )
        )
    else:
        last = M.ordered(t).tail(ks.rolling_window_trades)
        r = M.roi(last)
        out.append(
            KillSwitch(
                "rolling_roi",
                r is not None and r < thr,
                f"last {ks.rolling_window_trades} trades ROI {r:.3f} vs "
                f"bootstrap p{ks.rolling_roi_percentile:g} {thr:.3f}",
            )
        )
    if modeled_slippage is not None and daily_median_slippage:
        run = best = 0
        for s in daily_median_slippage:
            run = run + 1 if s > ks.slippage_ratio * modeled_slippage else 0
            best = max(best, run)
        out.append(
            KillSwitch(
                "slippage",
                best >= ks.slippage_consecutive_days,
                f"{best} consecutive days above {ks.slippage_ratio}x modeled",
            )
        )
    else:
        out.append(KillSwitch("slippage", False, "no daily slippage series yet"))
    run = best = 0
    for z in ev.epoch_pooled_z:
        run = run + 1 if z is not None and z < 0 else 0
        best = max(best, run)
    out.append(
        KillSwitch(
            "oos_z_negative",
            best >= ks.negative_oos_z_consecutive_epochs,
            f"{best} consecutive epochs with pooled OOS z < 0",
        )
    )
    bad = (
        ev.leakage_failures
        or ev.recent_drift_errors
        or (ev.config_hash_start and ev.config_hash_start != ev.config_hash_now)
    )
    out.append(
        KillSwitch(
            "data_integrity",
            bool(bad),
            f"leakage={ev.leakage_failures} drift={ev.recent_drift_errors} "
            f"config_changed={ev.config_hash_start != ev.config_hash_now}",
        )
    )
    return out


# --- micro-live (S3) evaluator: spec only, no trading code ---------------------------------


@dataclass(frozen=True)
class LiveStats:
    fills: int
    days: float
    signals: int
    realized_slippage: float
    modeled_slippage: float
    returns: tuple[float, ...]  # per-trade net return (pnl / stake)


def evaluate_micro_live(
    live: LiveStats, paper_returns: tuple[float, ...], cfg: Config, kill_switch_triggered: bool
) -> list[GateResult]:
    ml = cfg.micro_live
    ratio = (
        live.realized_slippage / live.modeled_slippage if live.modeled_slippage > 0 else math.inf
    )
    fr = live.fills / live.signals if live.signals else 0.0
    a, b = np.asarray(live.returns, float), np.asarray(paper_returns, float)
    if a.size >= 2 and b.size >= 2:
        se = math.sqrt(a.var(ddof=1) / a.size + b.var(ddof=1) / b.size)
        z = (a.mean() - b.mean()) / se if se > 0 else 0.0
        p_below = float(norm.cdf(z))  # small p => live significantly below paper
    else:
        z, p_below = float("nan"), 0.0
    return [
        GateResult(
            "S3_slippage",
            ratio <= ml.max_slippage_ratio,
            round(ratio, 3),
            ml.max_slippage_ratio,
            "realized / modeled slippage",
        ),
        GateResult("S3_fill_rate", fr >= ml.min_fill_rate, round(fr, 3), ml.min_fill_rate, ""),
        GateResult(
            "S3_live_vs_paper",
            p_below >= 0.05,
            {"z": z, "p": round(p_below, 4)},
            "p >= 0.05",
            "live ROI must not be significantly below paper ROI",
        ),
        GateResult(
            "S3_sample",
            live.fills >= ml.min_fills and live.days >= ml.min_days,
            {"fills": live.fills, "days": live.days},
            {"fills": ml.min_fills, "days": ml.min_days},
            "",
        ),
        GateResult(
            "S3_no_kill_switch", not kill_switch_triggered, kill_switch_triggered, False, ""
        ),
    ]
