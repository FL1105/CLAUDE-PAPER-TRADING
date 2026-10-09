"""Drawdown ladder with explicit re-risk and re-arm rules (pure state machine).

Same class is used by the backtest engine and the live runner so replays match.

Levels: FULL (x1.0) -> HALF (x0.5) at drawdown_halve_at -> CASH (x0) at drawdown_cash_at
        -> HALT (x0, flatten) at drawdown_halt_at.
Re-risk: HALF -> FULL when equity is within `rerisk_recover_within` of peak, or after
         `rerisk_min_days` AND a majority of assets have trend ON.
         CASH -> HALF after `halfrisk_return_days` with majority trend ON.
HALT: live = manual `rearm()` only. backtest = auto re-arm after `backtest_halt_rearm_days`.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.config import Risk

FULL, HALF, CASH, HALT = "FULL", "HALF", "CASH", "HALT"
MULT = {FULL: 1.0, HALF: 0.5, CASH: 0.0, HALT: 0.0}


@dataclass
class Ladder:
    risk: Risk
    auto_rearm: bool = True
    level: str = FULL
    peak: float = 1.0
    days_in_level: int = 0

    def update(self, equity: float, trend_majority: bool) -> float:
        """Advance one day using today's closing equity. Returns the risk multiplier."""
        r = self.risk
        self.peak = max(self.peak, equity)
        dd = 1.0 - equity / self.peak
        self.days_in_level += 1
        prev = self.level

        if self.level == HALT:
            if self.auto_rearm and self.days_in_level >= r.backtest_halt_rearm_days:
                self.peak = equity
                self.level = HALF  # come back at half risk
        elif dd >= r.drawdown_halt_at:
            self.level = HALT
        elif dd >= r.drawdown_cash_at:
            self.level = CASH
        elif dd >= r.drawdown_halve_at and self.level == FULL:
            self.level = HALF
        elif self.level == HALF:
            if dd <= r.rerisk_recover_within or (
                self.days_in_level >= r.rerisk_min_days and trend_majority
            ):
                self.level = FULL
        elif self.level == CASH:
            if self.days_in_level >= r.halfrisk_return_days and trend_majority:
                self.level = HALF
        if self.level != prev:
            self.days_in_level = 0
        return MULT[self.level]

    def rearm(self, equity: float) -> None:
        """Operator action (live). Resets the peak so the halt does not immediately retrigger."""
        self.level = HALF
        self.peak = equity
        self.days_in_level = 0

    @property
    def mult(self) -> float:
        return MULT[self.level]
