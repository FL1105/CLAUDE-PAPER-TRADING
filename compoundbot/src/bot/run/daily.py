"""Daily runner (idempotent): data -> quality -> signals -> weights -> ladder -> risk -> orders
-> submit -> reconcile -> write runs/<date>/.

Everything pure lives in `compute_targets`, shared with the research code, so a replay of past
days reproduces the backtest weights exactly."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from bot.broker.base import Broker
from bot.config import (
    CFG,
    ROOT,
    Costs,
    Risk,
    Universe,
    check_risk_hash,
    file_hash,
    load_costs,
    load_risk,
    load_strategy,
    load_universe,
)
from bot.data.calendar import nyse_sessions
from bot.portfolio.construct import combine
from bot.risk.drawdown import MULT, Ladder
from bot.risk.killswitch import KillSwitch
from bot.risk.limits import daily_loss_exceeded, data_healthy
from bot.run.orders import ExecReport, execute, plan_orders
from bot.run.reconcile import expected_after, reconcile
from bot.strategies import make_registry, split_universe

STATE_DIR = ROOT / "state"
RUNS_DIR = ROOT / "runs"


class RunLocked(RuntimeError):
    pass


def load_selected(path: Path | None = None) -> dict[str, dict]:
    """config/selected.yaml is written by the operator after Gate 1. {strategy: params}."""
    p = path or (CFG / "selected.yaml")
    if not p.exists():
        raise FileNotFoundError(f"{p} missing: the operator must select strategies after Gate 1")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not data.get("approved_by"):
        raise PermissionError(f"{p} has no approved_by: strategies are not signed off")
    return data["strategies"]


def compute_targets(close: pd.DataFrame, selected: dict[str, dict], universe: Universe,
                    risk: Risk) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pure: past prices -> final target weights (before the drawdown ladder)."""
    reg = make_registry(load_strategy())
    parts = split_universe(close, universe)
    sleeves = {}
    for name, params in selected.items():
        spec = reg[name]
        sleeves[name] = spec.build(close[parts[spec.universe]], params, risk)
    return combine(sleeves, close, universe, risk)


@dataclass
class RunState:
    ladder_level: str = "FULL"
    ladder_peak: float | None = None  # USD
    ladder_days: int = 0
    last_equity: float | None = None
    last_run_date: str | None = None
    start_of_day_equity: float | None = None
    paper_cash: float | None = None
    paper_positions: dict = field(default_factory=dict)


def load_state(path: Path) -> RunState:
    if path.exists():
        return RunState(**json.loads(path.read_text(encoding="utf-8")))
    return RunState()


def save_state(path: Path, st: RunState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(st), indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Lock:
    def __init__(self, path: Path, stale_seconds: float = 6 * 3600):
        self.path, self.stale = path, stale_seconds

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale:
            self.path.unlink()  # stale lock from a crashed run
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise RunLocked(f"another run holds {self.path}") from e
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=ROOT, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _inputs_hash(close: pd.DataFrame) -> str:
    return hashlib.sha256(close.tail(300).round(6).to_csv().encode()).hexdigest()[:16]


def session_lag(last_bar, today) -> int:
    last, today = pd.Timestamp(last_bar), pd.Timestamp(today)
    if last >= today:
        return 0
    return max(len(nyse_sessions(last, today)) - 1, 0)


@dataclass
class RunResult:
    run_id: str
    status: str  # ok | killed | data_unhealthy | halted | no_selection | error
    weights: dict = field(default_factory=dict)
    ladder: str = "FULL"
    report: ExecReport | None = None
    mismatches: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    equity: float | None = None


def run_daily(
    asof: pd.Timestamp, panel: dict, broker: Broker, *, selected: dict[str, dict] | None = None,
    universe: Universe | None = None, risk: Risk | None = None, costs: Costs | None = None,
    kill: KillSwitch | None = None, state_dir: Path | None = None, runs_dir: Path | None = None,
    require_risk_ack: bool = True, tag: str = "MEASURED", quotes: dict[str, float] | None = None,
) -> RunResult:
    universe, risk, costs = universe or load_universe(), risk or load_risk(), costs or load_costs()
    kill = kill or KillSwitch()
    state_dir, runs_dir = state_dir or STATE_DIR, runs_dir or RUNS_DIR
    run_id = pd.Timestamp(asof).strftime("%Y-%m-%d")
    notes: list[str] = []
    if require_risk_ack:
        check_risk_hash()
    with Lock(state_dir / "daily.lock"):
        st = load_state(state_dir / "state.json")
        close = panel["close"].loc[:asof]
        req_all = [s for s in universe.symbols if s in close.columns and close[s].notna().any()]
        px = quotes or {s: float(close[s].dropna().iloc[-1]) for s in req_all}
        if hasattr(broker, "set_quotes"):
            broker.set_quotes(px)
        acct = broker.get_account()
        res = RunResult(run_id=run_id, status="ok", equity=acct.equity)
        ladder = Ladder(risk, auto_rearm=False, level=st.ladder_level,
                        peak=st.ladder_peak or acct.equity, days_in_level=st.ladder_days)
        start_eq = st.start_of_day_equity if st.last_run_date == run_id else acct.equity
        if st.last_run_date != run_id:
            st.start_of_day_equity = acct.equity
            start_eq = acct.equity
        try:
            if kill.is_engaged():
                res.status, res.notes = "killed", [f"kill switch: {kill.info()}"]
                return _finish(res, st, state_dir, runs_dir, run_id, close, {}, tag, ladder)
            req = [s for s in universe.symbols if s in close.columns]
            latest = {s: close[s].dropna().index.max() for s in req if close[s].notna().any()}
            ok, bad = data_healthy(latest, [s for s in req if close[s].notna().any()], asof, 1,
                                   session_lag)
            if not ok:
                res.status, res.notes = "data_unhealthy", [f"stale or missing data: {bad}"]
                return _finish(res, st, state_dir, runs_dir, run_id, close, {}, tag, ladder)
            if selected is None:
                try:
                    selected = load_selected()
                except (FileNotFoundError, PermissionError) as e:
                    res.status, res.notes = "no_selection", [str(e)]
                    return _finish(res, st, state_dir, runs_dir, run_id, close, {}, tag, ladder)
            weights, _alloc = compute_targets(close, selected, universe, risk)
            base_row = weights.iloc[-1]
            # the ladder works on broker equity (USD) against its own running peak; once per day
            majority = bool((base_row > 1e-9).sum() >= max(1, 0.5 * close.iloc[-1].notna().sum()))
            mult = ladder.update(acct.equity, majority) if st.last_run_date != run_id \
                else MULT[ladder.level]
            res.ladder = ladder.level
            if ladder.level == "HALT":
                kill.engage(f"drawdown HALT at {run_id}", by="ladder")
                res.status = "halted"
            if daily_loss_exceeded(start_eq, acct.equity, risk):
                notes.append("daily loss halt: no new risk today")
                mult = min(mult, 0.0) if acct.equity < start_eq else mult
            target = {s: float(base_row.get(s, 0.0)) * mult for s in close.columns}
            res.weights = target
            positions = broker.get_positions()
            adv = {s: float(panel["volume"][s].dropna().tail(20).mean()) for s in req
                   if panel.get("volume") is not None and s in panel["volume"]
                   and panel["volume"][s].notna().any()}
            adv = {s: v for s, v in adv.items() if v > 0}
            orders, plan_notes = plan_orders(target, positions, px, acct.equity, risk, costs, universe,
                                             run_id, adv_usd=adv or None)
            notes += plan_notes
            if res.status == "halted":
                orders = [o for o in orders if o.side == "sell"]  # flatten only
            rep = execute(orders, broker, kill) if orders else ExecReport()
            res.report = rep
            res.mismatches = reconcile(expected_after(positions, rep.fills), broker.get_positions())
            if res.mismatches:
                notes.append(f"UNRECONCILED: {[asdict(m) for m in res.mismatches]}")
            res.equity = broker.get_account().equity
        except Exception as e:  # noqa: BLE001
            res.status, notes = "error", notes + [f"{type(e).__name__}: {e}"]
            raise
        finally:
            res.notes = notes
        return _finish(res, st, state_dir, runs_dir, run_id, close, res.weights, tag, ladder)


def _finish(res: RunResult, st: RunState, state_dir: Path, runs_dir: Path, run_id: str,
            close: pd.DataFrame, weights: dict, tag: str, ladder: Ladder) -> RunResult:
    st.ladder_level, st.ladder_peak, st.ladder_days = ladder.level, ladder.peak, ladder.days_in_level
    st.last_run_date = run_id
    st.last_equity = res.equity
    save_state(state_dir / "state.json", st)
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    rep = res.report
    (d / "run.json").write_text(json.dumps({
        "run_id": run_id, "status": res.status, "tag": tag, "ladder": res.ladder,
        "equity": res.equity, "weights": weights, "notes": res.notes,
        "orders": [asdict(o) for o in (rep.planned if rep else [])],
        "fills": [asdict(f) for f in (rep.fills if rep else [])],
        "mismatches": [asdict(m) for m in res.mismatches],
        "inputs_hash": _inputs_hash(close) if len(close) else "", "git": _git_commit(),
        "risk_hash": file_hash(CFG / "risk.yaml"),
    }, indent=2, default=float), encoding="utf-8")
    (state_dir / "heartbeat").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    return res
