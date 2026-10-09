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
from bot.portfolio.construct import book_rebalance, combine
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


LIVE_LOOKBACK_ROWS = 1000  # longest chain of rolling windows is ~450 sessions; this leaves a wide margin


def compute_targets(close: pd.DataFrame, selected: dict[str, dict], universe: Universe,
                    risk: Risk, tail: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pure: past prices -> final target weights (before the drawdown ladder).
    `tail` keeps only the last N rows (speed for the daily run). Rolling windows make the last
    row identical to the full-history result (tested)."""
    if tail:
        close = close.tail(tail)
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
    last_mult: float = 1.0
    start_of_day_equity: float | None = None
    pending_ids: list = field(default_factory=list)  # orders submitted last run, not yet filled
    positions_snapshot: dict = field(default_factory=dict)  # positions at the end of the last run


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


def _costs_hash() -> str:
    parts = [CFG / "costs.yaml", CFG / "costs_learned.yaml"]
    h = hashlib.sha256()
    for p in parts:
        if p.exists():
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _inputs_hash(close: pd.DataFrame) -> str:
    return hashlib.sha256(close.tail(300).round(6).to_csv().encode()).hexdigest()[:16]


def is_rebalance_day(asof, freq: str) -> bool:
    """True on the last NYSE session of the ISO week (freq "weekly") or every session ("daily")."""
    if freq == "daily":
        return True
    a = pd.Timestamp(asof).normalize()
    nxt = nyse_sessions(a + pd.Timedelta(days=1), a + pd.Timedelta(days=10))[0]
    return nxt.isocalendar()[:2] != a.isocalendar()[:2]


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
    settled: list = field(default_factory=list)


def run_daily(
    asof: pd.Timestamp, panel: dict, broker: Broker, *, selected: dict[str, dict] | None = None,
    universe: Universe | None = None, risk: Risk | None = None, costs: Costs | None = None,
    kill: KillSwitch | None = None, state_dir: Path | None = None, runs_dir: Path | None = None,
    require_risk_ack: bool = True, tag: str = "MEASURED", quotes: dict[str, float] | None = None,
    rebalance: str | None = None,
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
        opens = panel.get("open")
        open_px = ({s: float(opens[s].loc[:asof].dropna().iloc[-1]) for s in req_all
                    if s in opens and opens[s].loc[:asof].notna().any()}
                   if opens is not None else dict(px))
        first_run_of_day = st.last_run_date != run_id
        settled: list = []
        if first_run_of_day and st.pending_ids and st.last_run_date and \
                session_lag(st.last_run_date, asof) > 1:
            for oid in st.pending_ids:  # day orders from a missed session expire; never fill them late
                broker.cancel(oid)
            notes.append(f"expired {len(st.pending_ids)} stale queued orders from {st.last_run_date}")
            st.pending_ids = []
        if first_run_of_day and st.pending_ids:
            if kill.is_engaged():  # a kill switch cancels waiting BUYS; risk-reducing sells still go
                for oid in st.pending_ids:
                    o = broker.get_order(oid)
                    if o is not None and o.side == "buy":
                        broker.cancel(oid)
            if hasattr(broker, "settle"):
                broker.settle(open_px)  # simulator: yesterday's queued orders fill at today's open
            settled = [f for f in (broker.get_order(i) for i in st.pending_ids) if f is not None
                       and f.status != "open"]
            for f in settled:
                if not f.ref_price:
                    f.ref_price = open_px.get(f.symbol, 0.0)
        elif first_run_of_day and hasattr(broker, "settle"):
            broker.settle(open_px)
        if hasattr(broker, "set_quotes"):
            broker.set_quotes(px)
        acct = broker.get_account()
        res = RunResult(run_id=run_id, status="ok", equity=acct.equity)
        res.settled = settled
        if first_run_of_day and settled:
            res.mismatches = reconcile(expected_after(st.positions_snapshot, settled), broker.get_positions())
        elif first_run_of_day and st.positions_snapshot and not kill.is_engaged():
            res.mismatches = reconcile(st.positions_snapshot, broker.get_positions())
        if res.mismatches:
            notes.append(f"UNRECONCILED after settle: {[asdict(m) for m in res.mismatches]}")
        ladder = Ladder(risk, auto_rearm=False, level=st.ladder_level,
                        peak=st.ladder_peak or acct.equity, days_in_level=st.ladder_days)
        start_eq = st.start_of_day_equity if st.last_run_date == run_id else acct.equity
        if st.last_run_date != run_id:
            st.start_of_day_equity = acct.equity
            start_eq = acct.equity
        try:
            if kill.is_engaged():
                res.status, res.notes = "killed", [f"kill switch: {kill.info()}"]
                return _finish(res, st, state_dir, runs_dir, run_id, close, {}, tag, ladder, broker)
            req = [s for s in universe.symbols if s in close.columns]
            latest = {s: close[s].dropna().index.max() for s in req if close[s].notna().any()}
            ok, bad = data_healthy(latest, [s for s in req if close[s].notna().any()], asof, 1,
                                   session_lag)
            if not ok:
                res.status, res.notes = "data_unhealthy", [f"stale or missing data: {bad}"]
                return _finish(res, st, state_dir, runs_dir, run_id, close, {}, tag, ladder, broker)
            if selected is None:
                try:
                    selected = load_selected()
                except (FileNotFoundError, PermissionError) as e:
                    res.status, res.notes = "no_selection", [str(e)]
                    return _finish(res, st, state_dir, runs_dir, run_id, close, {}, tag, ladder, broker)
            weights, _alloc = compute_targets(close, selected, universe, risk, tail=LIVE_LOOKBACK_ROWS)
            base_row = weights.iloc[-1]
            # the ladder works on broker equity (USD) against its own running peak; once per day
            majority = bool((base_row > 1e-9).sum() >= max(1, 0.5 * close.iloc[-1].notna().sum()))
            mult = ladder.update(acct.equity, majority) if st.last_run_date != run_id \
                else MULT[ladder.level]
            res.ladder = ladder.level
            if (state_dir / "CASH_MODE").exists():  # set by drift monitor / operator
                mult = 0.0
                notes.append("CASH_MODE flag present: all targets scaled to 0")
            if ladder.level == "HALT":
                res.status = "halted"  # flatten first, then engage the kill switch (below)
            if daily_loss_exceeded(start_eq, acct.equity, risk):
                notes.append("daily loss halt: no new risk today")
                mult = min(mult, 0.0) if acct.equity < start_eq else mult
            target = {s: float(base_row.get(s, 0.0)) * mult for s in close.columns}
            res.weights = target
            positions = broker.get_positions()
            freq = rebalance or book_rebalance(selected, make_registry(load_strategy()))
            ladder_changed = first_run_of_day and mult != st.last_mult
            if first_run_of_day:
                st.last_mult = mult
            trade_today = is_rebalance_day(asof, freq) or ladder_changed or res.status == "halted"
            adv = {s: float(panel["volume"][s].dropna().tail(20).mean()) for s in req
                   if panel.get("volume") is not None and s in panel["volume"]
                   and panel["volume"][s].notna().any()}
            adv = {s: v for s, v in adv.items() if v > 0}
            orders, plan_notes = plan_orders(target, positions, px, acct.equity, risk, costs, universe,
                                             run_id, adv_usd=adv or None) if trade_today else ([], [])
            notes += plan_notes
            if not trade_today:
                notes.append(f"not a {freq} rebalance day: no orders")
            if res.status == "halted" or (state_dir / "PAUSE_ENTRIES").exists():
                orders = [o for o in orders if o.side == "sell"]  # flatten / no new entries
            rep = execute(orders, broker, kill) if orders else ExecReport()
            res.report = rep
            if res.status == "halted":
                kill.engage(f"drawdown HALT at {run_id}", by="ladder")
            # immediate fills (execution="immediate" brokers) must reconcile right away; queued ones
            # (status "open") are checked against the snapshot at the next session's settle step
            immediate = [f for f in rep.fills if f.status != "open"]
            for f in immediate:
                if not f.ref_price:
                    f.ref_price = px.get(f.symbol, 0.0)
            res.mismatches += reconcile(expected_after(positions, immediate), broker.get_positions())
            st.pending_ids = [f.client_order_id for f in rep.fills if f.status == "open"] + \
                [i for i in st.pending_ids if broker.get_order(i) and broker.get_order(i).status == "open"
                 and i not in {f.client_order_id for f in rep.fills}]
            if res.mismatches:
                notes.append(f"UNRECONCILED: {[asdict(m) for m in res.mismatches]}")
            res.equity = broker.get_account().equity
        except Exception as e:  # noqa: BLE001
            res.status, notes = "error", notes + [f"{type(e).__name__}: {e}"]
            raise
        finally:
            res.notes = notes
        return _finish(res, st, state_dir, runs_dir, run_id, close, res.weights, tag, ladder, broker)


def _finish(res: RunResult, st: RunState, state_dir: Path, runs_dir: Path, run_id: str,
            close: pd.DataFrame, weights: dict, tag: str, ladder: Ladder, broker: Broker) -> RunResult:
    st.ladder_level, st.ladder_peak, st.ladder_days = ladder.level, ladder.peak, ladder.days_in_level
    st.last_run_date = run_id
    st.last_equity = res.equity
    st.positions_snapshot = broker.get_positions()
    save_state(state_dir / "state.json", st)
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    rep = res.report
    (d / "run.json").write_text(json.dumps({
        "run_id": run_id, "status": res.status, "tag": tag, "ladder": res.ladder,
        "equity": res.equity, "weights": weights, "notes": res.notes,
        "broker": broker.__class__.__name__, "costs_hash": _costs_hash(),
        "orders": [asdict(o) for o in (rep.planned if rep else [])],
        "fills": [asdict(f) for f in list(res.settled) + [f for f in (rep.fills if rep else [])
                                                         if f.status != "open"]],
        "queued": [asdict(f) for f in (rep.fills if rep else []) if f.status == "open"],
        "mismatches": [asdict(m) for m in res.mismatches],
        "inputs_hash": _inputs_hash(close) if len(close) else "", "git": _git_commit(),
        "risk_hash": file_hash(CFG / "risk.yaml"),
    }, indent=2, default=float), encoding="utf-8")
    (state_dir / "heartbeat").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    return res

