"""Read-only data access for the dashboard (runs/, state/, reports/)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.monitor.weekly_review import load_runs
from bot.risk.killswitch import KillSwitch


def summary(root: Path) -> dict:
    runs = load_runs(root / "runs")
    ks = KillSwitch(root / "state" / "KILL")
    last = {}
    if len(runs):
        p = root / "runs" / runs.index[-1].strftime("%Y-%m-%d") / "run.json"
        last = json.loads(p.read_text(encoding="utf-8"))
    state_p = root / "state" / "state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    paused = (root / "state" / "PAUSE_ENTRIES").exists()
    dp = root / "state" / "drift.json"
    drift = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else None
    eq = runs["equity"].astype(float) if len(runs) else pd.Series(dtype=float)
    dd = float(1 - eq.iloc[-1] / eq.cummax().iloc[-1]) if len(eq) else 0.0
    return {
        "tag": last.get("tag", "none"), "last_run": last.get("run_id"), "status": last.get("status"),
        "equity": float(eq.iloc[-1]) if len(eq) else None, "drawdown": dd,
        "ladder": state.get("ladder_level", "FULL"), "kill_switch": ks.info(), "paused": paused,
        "cash_mode": (root / "state" / "CASH_MODE").exists(), "drift": drift, "n_runs": int(len(runs)),
        "notes": last.get("notes", []), "inputs_hash": last.get("inputs_hash"),
        "heartbeat": (root / "state" / "heartbeat").read_text() if (root / "state" / "heartbeat").exists() else None,
    }


def equity(root: Path) -> dict:
    runs = load_runs(root / "runs")
    if runs.empty:
        return {"dates": [], "equity": [], "drawdown": [], "benchmarks": {}}
    eq = runs["equity"].astype(float)
    bj = root / "reports" / "benchmarks.json"
    bench = json.loads(bj.read_text(encoding="utf-8")) if bj.exists() else {}
    return {"dates": [d.strftime("%Y-%m-%d") for d in eq.index], "equity": eq.round(2).tolist(),
            "drawdown": (eq / eq.cummax() - 1).round(5).tolist(), "benchmarks": bench}


def weights(root: Path) -> dict:
    runs = load_runs(root / "runs")
    if runs.empty:
        return {"target": {}, "dates": []}
    p = root / "runs" / runs.index[-1].strftime("%Y-%m-%d") / "run.json"
    r = json.loads(p.read_text(encoding="utf-8"))
    return {"target": {k: round(v, 4) for k, v in r.get("weights", {}).items() if v},
            "date": r["run_id"]}


def trades(root: Path, q: str = "", limit: int = 500) -> list[dict]:
    rows = []
    for p in sorted((root / "runs").glob("*/run.json"), reverse=True):
        r = json.loads(p.read_text(encoding="utf-8"))
        for f in r.get("fills", []):
            row = {"date": r["run_id"], **f}
            if not q or q.lower() in json.dumps(row).lower():
                rows.append(row)
        if len(rows) >= limit:
            break
    return rows[:limit]


def gate(root: Path) -> dict:
    p = root / "reports" / "gate_status.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"edge": [], "implementation": []}
