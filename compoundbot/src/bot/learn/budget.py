"""Research budget (a simple form of alpha-spending).

The experiment log (runs/experiments_<tag>.jsonl) is the single source of truth:
  stage "founding" : a pre-registered grid combination, logged the first time a strategy is researched (free)
  stage "charge"   : a NEW combination (strategy+params never seen) that spent budget, with the date
  stage "trial"    : an evaluation of any combination on a data window (the existing record)
Budget  : at most `new_combos_per_quarter` charges in a calendar quarter.
Bar     : the Deflated-Sharpe threshold rises by `dsr_step` per `dsr_step_every` charges used this quarter.
The trial count N that feeds the Deflated Sharpe already includes every combination ever tried, so the
longer the bot has been searching, the higher the Sharpe a new variant must show.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from bot.config import ResearchBudget


class BudgetExceeded(RuntimeError):
    pass


def combo_key(strategy: str, params: dict) -> str:
    return f"{strategy}|{json.dumps(params, sort_keys=True)}"


def quarter_of(ts) -> str:
    t = pd.Timestamp(ts)
    return f"{t.year}Q{(t.month - 1) // 3 + 1}"


class Budget:
    def __init__(self, cfg: ResearchBudget, log_path: Path):
        self.cfg, self.path = cfg, Path(log_path)

    def _records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def known(self) -> set[str]:
        return {combo_key(r["strategy"], r["params"]) for r in self._records()
                if r.get("stage") in ("trial", "founding", "charge")}

    def strategies_seen(self) -> set[str]:
        return {r["strategy"] for r in self._records() if r.get("stage") in ("trial", "founding", "charge")}

    def used(self, now=None) -> int:
        q = quarter_of(now or time.strftime("%Y-%m-%d"))
        return sum(1 for r in self._records() if r.get("stage") == "charge" and r.get("quarter") == q)

    def remaining(self, now=None) -> int:
        return max(self.cfg.new_combos_per_quarter - self.used(now), 0)

    def dsr_min(self, now=None) -> float:
        steps = self.used(now) // self.cfg.dsr_step_every
        return min(self.cfg.dsr_min_base + self.cfg.dsr_step * steps, self.cfg.dsr_min_cap)

    def reserve(self, strategy: str, combos: list[dict], data_id: str = "", now=None,
                dry_run: bool = False) -> dict:
        """Account for the combinations about to be evaluated. Returns {'founding': n, 'new': n, 'known': n}.
        Raises BudgetExceeded (before anything is logged) if the new ones do not fit."""
        now = now or time.strftime("%Y-%m-%d")
        known = self.known()
        founding = strategy not in self.strategies_seen()
        fresh = [c for c in combos if combo_key(strategy, c) not in known]
        out = {"founding": len(fresh) if founding else 0, "new": 0 if founding else len(fresh),
               "known": len(combos) - len(fresh)}
        if out["new"] > self.remaining(now):
            raise BudgetExceeded(
                f"{strategy}: {out['new']} new variants but only {self.remaining(now)} left in "
                f"{quarter_of(now)} (budget {self.cfg.new_combos_per_quarter}). Wait for next quarter, "
                f"drop variants, or have the operator raise the budget in config/research_budget.yaml "
                f"and log why in docs/DECISIONS.md.")
        if dry_run:
            return out
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            for c in fresh:
                rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "strategy": strategy, "params": c,
                       "data_id": data_id, "stage": "founding" if founding else "charge",
                       "quarter": quarter_of(now)}
                f.write(json.dumps(rec) + "\n")
        return out

    def status(self, now=None) -> dict:
        now = now or time.strftime("%Y-%m-%d")
        return {"quarter": quarter_of(now), "budget": self.cfg.new_combos_per_quarter, "used": self.used(now),
                "remaining": self.remaining(now), "dsr_min": self.dsr_min(now),
                "combos_ever": len(self.known())}
