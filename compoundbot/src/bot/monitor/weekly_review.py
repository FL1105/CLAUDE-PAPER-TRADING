"""Weekly review: writes reports/weekly_<date>.md from run records. No LLM call happens here.

Hypothesis proposals (from a human or an LLM) are only QUEUED after strict validation. A proposal
can name a registered strategy and grid values, and nothing else: it can never touch risk.yaml,
the evidence gate, live trading, or any file path. Queued items must be run through the
walk-forward harness and need a challenger-beats-champion test (below) before use."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from bot.backtest import stats

FORBIDDEN = ("risk", "gate", "live", "kill", "path", "file", "limit", "enable", "key", "secret")
MAX_PROPOSALS_PER_WEEK = 3


class ProposalRejected(ValueError):
    pass


def validate_proposal(p: dict, registry: dict) -> dict:
    if set(p) - {"strategy", "grid", "rationale"}:
        raise ProposalRejected(f"unknown fields: {sorted(set(p) - {'strategy', 'grid', 'rationale'})}")
    name = p.get("strategy")
    if name not in registry:
        raise ProposalRejected(f"unknown strategy {name!r}")
    grid = p.get("grid")
    if not isinstance(grid, dict) or not grid:
        raise ProposalRejected("grid must be a non-empty dict")
    allowed = set(registry[name].grid)
    for k, v in grid.items():
        if k not in allowed:
            raise ProposalRejected(f"parameter {k!r} is not tunable for {name}")
        if any(f in k.lower() for f in FORBIDDEN):
            raise ProposalRejected(f"parameter {k!r} is forbidden")
        if not isinstance(v, list) or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            raise ProposalRejected(f"{k}: values must be a list of numbers")
        if len(v) > 5:
            raise ProposalRejected(f"{k}: too many values (max 5)")
    return {"strategy": name, "grid": grid, "rationale": str(p.get("rationale", ""))[:500]}


def queue_hypotheses(proposals: list[dict], registry: dict, queue_path: Path) -> list[dict]:
    """Validate and append at most 3 proposals. Returns the accepted ones; rejected ones raise nothing
    but are recorded in the returned list's absence and in the queue file's `rejected` entries."""
    accepted = []
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with open(queue_path, "a", encoding="utf-8") as f:
        for p in proposals[:MAX_PROPOSALS_PER_WEEK]:
            try:
                ok = validate_proposal(p, registry)
                accepted.append(ok)
                f.write(json.dumps({"status": "queued", **ok}) + "\n")
            except ProposalRejected as e:
                f.write(json.dumps({"status": "rejected", "reason": str(e)}) + "\n")
    return accepted


def challenger_beats_champion(champ_x1: pd.Series, chall_x1: pd.Series, champ_x2: pd.Series,
                              chall_x2: pd.Series, n_boot: int = 1000, seed: int = 7) -> bool:
    """Replace only if the lower 95% bound of the OOS mean-return improvement is > 0 at x1 AND x2 costs."""
    for c, h in ((champ_x1, chall_x1), (champ_x2, chall_x2)):
        d = stats.paired_bootstrap_diff(h, c, n_boot=n_boot, seed=seed, metric="mean")
        if not d["ci"][0] > 0:
            return False
    return True


def load_runs(runs_dir: Path) -> pd.DataFrame:
    rows = []
    for p in sorted(runs_dir.glob("*/run.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        cost = sum(f.get("cost_usd", 0.0) for f in r.get("fills", []))
        rows.append({"date": pd.Timestamp(r["run_id"]), "equity": r.get("equity"), "status": r["status"],
                     "ladder": r.get("ladder"), "cost_usd": cost, "n_fills": len(r.get("fills", [])),
                     "mismatches": len(r.get("mismatches", [])), "tag": r.get("tag", "")})
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def write_weekly(runs_dir: Path, out_dir: Path, benchmarks: dict[str, pd.Series] | None = None,
                 asof: pd.Timestamp | None = None, drift_note: str = "") -> Path:
    df = load_runs(runs_dir)
    asof = asof or (df.index.max() if len(df) else pd.Timestamp.today().normalize())
    lines = [f"# Weekly review {asof.date()}", ""]
    if df.empty:
        lines.append("No runs recorded yet.")
    else:
        wk = df.loc[df.index > asof - pd.Timedelta(days=7)]
        tag = ", ".join(sorted(set(df["tag"])))
        lines += [f"Data tag(s): {tag}", ""]
        eq = df["equity"].astype(float)
        ret = eq.pct_change().dropna()
        lines += ["## Performance", f"- Equity: {eq.iloc[-1]:,.2f} (start {eq.iloc[0]:,.2f}); "
                  f"total {eq.iloc[-1] / eq.iloc[0] - 1:+.2%} over {len(eq)} runs",
                  f"- This week: {(wk['equity'].iloc[-1] / wk['equity'].iloc[0] - 1) if len(wk) > 1 else 0.0:+.2%}"]
        for name, s in (benchmarks or {}).items():
            s = s.reindex(eq.index).dropna()
            if len(s) > 1:
                lines.append(f"- {name}: {s.iloc[-1] / s.iloc[0] - 1:+.2%} over the same dates")
        lines += ["", "## Risk state", f"- Ladder: {df['ladder'].iloc[-1]}",
                  f"- Runs not ok: {int((df['status'] != 'ok').sum())}; "
                  f"unreconciled: {int(df['mismatches'].sum())}",
                  "", "## Costs", f"- Paid in the period: {df['cost_usd'].sum():,.2f} USD over "
                  f"{int(df['n_fills'].sum())} fills",
                  f"- Realised daily vol: {ret.std() * np.sqrt(252):.1%} annualised" if len(ret) > 2 else "",
                  "", "## Drift", drift_note or "- not evaluated"]
    lines += ["", "## What could I be wrong about?",
              "- Paper fills are idealised; paper-vs-backtest tracking tests plumbing, not real execution.",
              "- A few weeks of results cannot confirm or refute an edge (Sharpe SE ~ 1/sqrt(years))."]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly_{asof.date()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
