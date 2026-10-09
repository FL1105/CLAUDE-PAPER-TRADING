"""Evidence-gate bookkeeping: implementation criteria I1-I4 from paper runs, and a combined
checklist that includes the edge criteria saved by the research run. Reports value / threshold /
PASS-FAIL / sample size. Thresholds are fixed here; they are not edited after seeing results."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

TE_LIMIT_BPS = 15.0
COST_TOLERANCE = 0.25
VOL_TOLERANCE = 0.25


def implementation_gate(runs: pd.DataFrame, paper_days: int, replay_net: pd.Series | None,
                        modelled_cost_usd: float | None, target_vol: float,
                        drills: dict | None) -> list[dict]:
    out = []

    def add(cid, desc, value, thr, ok, n):
        out.append({"id": cid, "desc": desc, "value": value, "threshold": thr, "pass": bool(ok), "n": n})

    n = len(runs)
    if n:
        errors = int((runs["status"].isin(["error", "halted"])).sum())
        unrec = int(runs["mismatches"].sum())
        add("I1", f"{paper_days} trading days, no errors/halts, zero unreconciled positions",
            f"{n} days, {errors} error/halt runs, {unrec} unreconciled", f">= {paper_days} days, 0, 0",
            n >= paper_days and errors == 0 and unrec == 0, n)
    else:
        add("I1", f"{paper_days} trading days of paper runs", "0 days", f">= {paper_days}", False, 0)
    eq = runs["equity"].astype(float) if n else pd.Series(dtype=float)
    ret = eq.pct_change().dropna()
    if replay_net is not None and len(ret) > 5:
        j = pd.concat([ret, replay_net], axis=1, join="inner").dropna()
        te = float((j.iloc[:, 0] - j.iloc[:, 1]).std(ddof=1) * 1e4) if len(j) > 2 else float("nan")
        paid = float(runs["cost_usd"].sum())
        cost_ok = modelled_cost_usd is not None and modelled_cost_usd > 0 and \
            abs(paid - modelled_cost_usd) <= COST_TOLERANCE * modelled_cost_usd
        add("I2", "Daily tracking error vs backtest replay < 15 bps; cost drag within 25% of modelled",
            f"TE {te:.1f} bps; cost paid {paid:.2f} vs modelled {modelled_cost_usd}", f"<{TE_LIMIT_BPS} bps, +/-25%",
            np.isfinite(te) and te < TE_LIMIT_BPS and cost_ok, len(j))
    else:
        add("I2", "Tracking error vs replay", "not enough data", f"<{TE_LIMIT_BPS} bps", False, len(ret))
    if drills:
        ok = all(drills.get(k) for k in ("kill_switch", "drawdown_halt", "crash_recovery"))
        add("I3", "Kill-switch, drawdown-halt and crash-recovery drills pass", str(drills), "all True", ok, 3)
    else:
        add("I3", "Drills", "not run (python -m bot.cli drills)", "all True", False, 0)
    if len(ret) > 20:
        vol = float(ret.std(ddof=1) * np.sqrt(252))
        ratio = vol / target_vol
        se = 1 / np.sqrt(2 * len(ret))  # relative error of a vol estimate
        add("I4", "Realised vol within +/-25% of target", f"{vol:.1%} ({ratio:.0%} of target; est. rel. error "
            f"+/-{1.96 * se:.0%})", "75%-125%", abs(ratio - 1) <= VOL_TOLERANCE, len(ret))
    else:
        add("I4", "Realised vol vs target", "not enough data", "75%-125%", False, len(ret))
    return out


def combined_checklist(reports_dir: Path, impl: list[dict]) -> dict:
    edge, kept_ok, kept = [], True, 0
    rj = reports_dir / "research_v1.json"
    tag = ""
    if rj.exists():
        data = json.loads(rj.read_text(encoding="utf-8"))
        tag = data.get("tag", "")
        for name, o in data.get("outcomes", {}).items():
            for c in o["criteria"]:
                edge.append({**c, "strategy": f"{name} ({o['verdict']})"})
            if o["verdict"] == "KEEP":  # REJECTED strategies are not in the book and do not block the gate
                kept += 1
                kept_ok &= all(c["pass"] for c in o["criteria"])
    hj = reports_dir / "holdout.json"
    holdout = json.loads(hj.read_text(encoding="utf-8")) if hj.exists() else None
    holdout_ok = bool(holdout and holdout.get("E7_pass"))
    return {"edge": edge, "implementation": impl, "holdout": holdout, "research_tag": tag,
            "kept_strategies": kept,
            "all_pass": kept > 0 and kept_ok and holdout_ok and all(c["pass"] for c in impl)
            and tag == "MEASURED"}
