"""One learning cycle: take queued proposals, spend research budget on them, screen with the full
walk-forward harness (with the raised Deflated-Sharpe bar), and register survivors as SHADOW challengers.
It never edits selected.yaml, risk limits, the gate or any live switch."""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from bot.backtest.walkforward import key, research
from bot.config import ResearchBudget, Risk, Strategy
from bot.learn import shadow as shadow_mod
from bot.learn.budget import Budget, BudgetExceeded
from bot.monitor.weekly_review import mark_evaluated, pending_proposals
from bot.strategies import Spec, make_registry


def extend_spec(spec: Spec, grid_add: dict[str, list]) -> Spec:
    """Union of the pre-registered grid and the proposed values (original order, new values appended)."""
    grid = {k: list(v) for k, v in spec.grid.items()}
    for k, vals in grid_add.items():
        for v in vals:
            if v not in grid[k]:
                grid[k].append(v)
    return replace(spec, grid=grid)


def run_cycle(panel: dict, tag: str, data_id: str, universe, risk: Risk, costs, strat_cfg: Strategy,
              bcfg: ResearchBudget, exp_path: Path, queue_path: Path, shadows_path: Path,
              selected: dict[str, dict], report_path: Path, now: str | None = None,
              n_placebo: int = 200, n_boot: int = 500) -> dict:
    now = now or time.strftime("%Y-%m-%d")
    budget = Budget(bcfg, exp_path)
    reg = make_registry(strat_cfg)
    data_through = str(panel["close"].dropna(how="all").index.max().date())
    log: list[dict] = []
    before = budget.status(now)
    queue = pending_proposals(queue_path)[: bcfg.max_proposals_per_cycle]
    for prop in queue:
        name, pid = prop["strategy"], prop["id"]
        entry = {"id": pid, "strategy": name, "grid": prop["grid"], "result": ""}
        log.append(entry)
        if name not in selected:
            entry["result"] = "skipped: strategy is not in the selected book (no champion to challenge)"
            mark_evaluated(queue_path, pid, "skipped_no_champion")
            continue
        spec = extend_spec(reg[name], prop["grid"])
        try:
            res = research(panel, universe, risk, costs, {name: spec}, strat_cfg.walkforward, exp_path, tag,
                           data_id, n_placebo=n_placebo, n_boot=n_boot, only=[name], budget=budget, now=now)
        except BudgetExceeded as e:
            entry["result"] = f"deferred (stays queued): {e}"
            continue
        oc = res["outcomes"].get(name)
        if oc is None:
            entry["result"] = "no out-of-sample window"
            mark_evaluated(queue_path, pid, "no_oos")
            continue
        new_vals = {k: set(v) for k, v in prop["grid"].items()}
        uses_new = any(oc.final_params.get(k) in vs for k, vs in new_vals.items())
        same = key(oc.final_params) == key(selected[name])
        entry.update({"verdict": oc.verdict, "final_params": oc.final_params,
                      "dsr_bar": res["dsr_min"], "n_trials": res["n_trials"]})
        if oc.verdict != "KEEP":
            entry["result"] = "rejected by the evidence gate (E1-E6) at the raised bar"
            mark_evaluated(queue_path, pid, "rejected_gate")
        elif same or not uses_new:
            entry["result"] = "gate passed but the chosen parameters are not new: nothing to challenge with"
            mark_evaluated(queue_path, pid, "no_change")
        else:
            sh = shadow_mod.register(shadows_path, name, oc.final_params, selected[name], data_through, pid, now)
            entry["result"] = f"registered as shadow {sh['id']} (data through {data_through})"
            mark_evaluated(queue_path, pid, f"shadow:{sh['id']}")
    after = budget.status(now)
    _write_report(report_path, now, tag, before, after, log, data_through)
    return {"log": log, "before": before, "after": after}


def _write_report(path: Path, now: str, tag: str, before: dict, after: dict, log: list[dict], through: str) -> None:
    lines = [f"# Learning cycle {now} ({tag})", "",
             "SYNTHETIC data: mechanics only." if tag == "SYNTHETIC" else "",
             f"Data through {through}. Research budget {after['quarter']}: used {before['used']} -> {after['used']} "
             f"of {after['budget']}; Deflated-Sharpe bar {before['dsr_min']:.2f} -> {after['dsr_min']:.2f}; "
             f"combinations ever tried: {after['combos_ever']}.", ""]
    if not log:
        lines.append("No queued proposals. Nothing was tested, so nothing was spent.")
    for e in log:
        lines += [f"## {e['strategy']} {e['grid']} (proposal {e['id']})", f"- {e['result']}"]
        if "verdict" in e:
            lines.append(f"- verdict {e['verdict']}, chosen params {e['final_params']}, trials counted {e['n_trials']}")
    lines += ["", "A registered shadow challenger earns nothing by itself: it must beat the champion on sessions "
              "after its data-through date (`python -m bot.cli shadow`), then you decide (`promote`).", "",
              "## WHAT COULD I BE WRONG ABOUT?",
              "- Every proposal is another look at the same history; the budget limits this but cannot remove it.",
              "- A walk-forward gate on past data cannot distinguish a real improvement from a lucky one; "
              "only the shadow period can."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
