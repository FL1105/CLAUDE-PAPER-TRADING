"""Automated drills for gate criterion I3: kill switch, drawdown halt, crash recovery.
Each runs against the SYNTHETIC world in a temp folder and returns True/False."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from bot.broker.paper_local import PaperLocalBroker
from bot.config import load_costs, load_risk, load_universe
from bot.data.synthetic import make_prices
from bot.risk.killswitch import KillSwitch
from bot.run.daily import RunLocked, run_daily

SEL = {"trend": {"sma": 100, "vol_window": 60}}


def _world():
    p = make_prices(end="2009-12-31", seed=21)
    return {k: p[k] for k in ("close", "open", "cash", "volume")}


def _env(tmp: Path):
    u, c, r = load_universe(), load_costs(), load_risk()
    return u, c, r, PaperLocalBroker(c, u), KillSwitch(tmp / "KILL")


def drill_kill_switch() -> bool:
    w = _world()
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        u, c, r, br, ks = _env(tmp)
        kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp / "s", runs_dir=tmp / "r",
                  rebalance="daily")
        idx = w["close"].index
        ks.engage("drill", by="drill")
        a = run_daily(idx[-3], w, br, **kw)
        blocked = a.status == "killed" and not br.get_positions() and not br.pending
        ks.release("drill")
        run_daily(idx[-2], w, br, **kw)  # signal and queue
        b = run_daily(idx[-1], w, br, **kw)  # fills at the open
        return blocked and b.status == "ok" and bool(br.get_positions())


def drill_drawdown_halt() -> bool:
    w = _world()
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        u, c, r, br, ks = _env(tmp)
        kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp / "s", runs_dir=tmp / "r",
                  rebalance="daily")
        idx = w["close"].index
        run_daily(idx[-14], w, br, **kw)
        run_daily(idx[-13], w, br, **kw)
        held = bool(br.get_positions())
        # crush account equity by 18% (more than the halt level) before the next run
        br.cash -= 0.18 * br.get_account().equity
        res = run_daily(idx[-12], w, br, **kw)
        queued = [o for o in br.pending]
        sold_only = bool(queued) and all(o.side == "sell" for o in queued)
        after = run_daily(idx[-11], w, br, **kw)  # kill switch engaged by the ladder; sells settle at the open
        flat = not br.get_positions()
        return held and res.status == "halted" and ks.is_engaged() and sold_only and flat and \
            after.status == "killed" and not br.pending


def drill_crash_recovery() -> bool:
    w = _world()
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        u, c, r, br, ks = _env(tmp)
        kw = dict(selected=SEL, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp / "s", runs_dir=tmp / "r",
                  rebalance="daily")
        idx = w["close"].index
        # (a) a crashed process leaves a stale lock behind: the next run clears it and proceeds
        lock = tmp / "s" / "daily.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999")
        old = time.time() - 24 * 3600
        os.utime(lock, (old, old))
        a = run_daily(idx[-20], w, br, **kw)  # queues orders
        # (b) a LIVE lock blocks a concurrent run
        lock.write_text("1")
        try:
            run_daily(idx[-19], w, br, **kw)
            concurrent_blocked = False
        except RunLocked:
            concurrent_blocked = True
        lock.unlink()
        # (c) an exception mid-run still releases the lock; the next run recovers and reconciles
        class Boom(PaperLocalBroker):
            def submit_order(self, order):
                raise ConnectionError("broker down")
        bad = Boom(c, u, cash=br.cash)
        bad.positions = dict(br.positions)
        try:
            run_daily(idx[-10], w, bad, **kw)
        except ConnectionError:
            pass
        lock_free = not lock.exists()
        b = run_daily(idx[-1], w, br, **kw)
        return a.status == "ok" and concurrent_blocked and lock_free and b.status == "ok" and not b.mismatches
