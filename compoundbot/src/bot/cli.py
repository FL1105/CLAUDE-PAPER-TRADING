"""Command line interface: python -m bot.cli <command>. See docs/RUNBOOK.md."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

from bot import config as C
from bot.backtest import metrics, stats
from bot.backtest.baselines import run_baselines
from bot.backtest.portfolio_report import build_portfolio, portfolio_md
from bot.backtest.report import baselines_md, research_md
from bot.backtest.walkforward import research, split_holdout
from bot.data import panel as P
from bot.risk.killswitch import KillSwitch
from bot.strategies import make_registry

ROOT = C.ROOT
REPORTS = ROOT / "reports"


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        import os
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def cmd_data(a) -> int:
    u = C.load_universe()
    prov = P.choose_provider(a.source, seed=a.seed, planted_edge=not a.null)
    info = P.fetch_and_cache(prov, u, a.start, a.end, report_path=REPORTS / "data_quality.md")
    unexplained = [f for f in info["findings"] if not f.explained]
    print(f"cached {info['tag']} data; quality findings: {len(unexplained)} (see reports/data_quality.md)")
    return 0


def _dev(panel_meta):
    panel, meta = panel_meta
    cfg = C.load_strategy()
    dev, hold, cut = split_holdout(panel, cfg.walkforward["holdout_years"])
    return panel, meta, dev, hold, cut, cfg


def cmd_research(a) -> int:
    u, risk, costs = C.load_universe(), C.load_risk(), C.load_costs()
    panel, meta, dev, hold, cut, cfg = _dev(P.load_cached_panel(u))
    tag, did = meta["tag"], meta.get("data_id", "")
    exec_mode = cfg.walkforward["exec_mode"]
    exp = ROOT / "runs" / f"experiments_{tag}.jsonl"
    reg = make_registry(cfg)
    from bot.learn.budget import Budget, BudgetExceeded
    budget = Budget(C.load_budget(), exp)
    try:
        res = research(dev, u, risk, costs, reg, cfg.walkforward, exp, tag, did, n_placebo=a.placebo,
                       n_boot=a.boot, cover_min=2.0, only=a.only.split(",") if a.only else None, budget=budget)
    except BudgetExceeded as e:
        print(f"Research budget: {e}")
        return 1
    cash = res["cash"]
    baselines_md(res["baselines"], cash, tag, costs, exec_mode, REPORTS / "baselines.md")
    note = f"Holdout ({cfg.walkforward['holdout_years']}y from {cut.date()}) was NOT used."
    research_md(res, costs, exec_mode, note, REPORTS / "research_v1.md")
    out = {"tag": tag, "data_id": did, "n_trials": res["n_trials"],
           "outcomes": {n: {"verdict": o.verdict, "params": o.final_params, "criteria": o.criteria,
                            "oos_sharpe": metrics.sharpe(o.oos_net, cash)} for n, o in res["outcomes"].items()}}
    (REPORTS / "research_v1.json").write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    for n, o in res["outcomes"].items():
        print(f"{n:20s} {o['verdict'] if isinstance(o, dict) else o.verdict}  {o.final_params}")
    print(f"reports written to {REPORTS} ({tag}). Gate 1: review reports/research_v1.md, then `select`.")
    return 0


def _exp_path(tag: str) -> Path:
    return ROOT / "runs" / f"experiments_{tag}.jsonl"


def _cache_tag() -> str:
    _, meta = P.load_cached_panel()
    return meta["tag"]


def cmd_budget(a) -> int:
    from bot.learn.budget import Budget
    b = Budget(C.load_budget(), _exp_path(_cache_tag()))
    print(json.dumps(b.status(), indent=2))
    return 0


def cmd_propose(a) -> int:
    from bot.monitor.weekly_review import queue_hypotheses
    props = json.loads(Path(a.file).read_text(encoding="utf-8"))
    acc = queue_hypotheses(props if isinstance(props, list) else [props], make_registry(C.load_strategy()),
                           REPORTS / "hypotheses_queue.jsonl")
    print(f"queued {len(acc)} of {len(props)} (max 3 per call; duplicates and invalid ones are dropped, "
          f"see reports/hypotheses_queue.jsonl)")
    return 0


def cmd_learn(a) -> int:
    from bot.learn.cycle import run_cycle
    u, risk, costs = C.load_universe(), C.load_risk(), C.load_costs()
    panel, meta = P.load_cached_panel(u)
    cfg = C.load_strategy()
    sel = _selected_or_exit(a)
    tag = meta["tag"]
    out = run_cycle(panel, tag, meta.get("data_id", ""), u, risk, costs, cfg, C.load_budget(), _exp_path(tag),
                    REPORTS / "hypotheses_queue.jsonl", ROOT / "state" / "shadows.json", sel,
                    REPORTS / f"learning_cycle_{pd.Timestamp.today().date()}.md",
                    n_placebo=a.placebo, n_boot=a.boot)
    for e in out["log"]:
        print(f"{e['id']} {e['strategy']} {e['grid']}: {e['result']}")
    print(f"budget: {out['after']}")
    return 0


def _shadow_evals(a):
    from bot.learn import shadow as S
    u, risk, costs = C.load_universe(), C.load_risk(), C.load_costs()
    panel, meta = P.load_cached_panel(u)
    cfg = C.load_strategy()
    sel = _selected_or_exit(a)
    items = [i for i in S.load_shadows(ROOT / "state" / "shadows.json") if i["status"] == "shadowing"]
    return [(i, S.evaluate(panel, i, sel, u, risk, costs, cfg.walkforward["exec_mode"], C.load_budget(),
                           meta["tag"])) for i in items]


def cmd_shadow(a) -> int:
    evals = _shadow_evals(a)
    if not evals:
        print("no shadow challengers")
    for i, ev in evals:
        print(f"{i['id']}: {ev['status']} ({ev.get('new_sessions', 0)}/{ev['needed'] if 'needed' in ev else '?'} "
              f"new sessions) {ev.get('reason', '')}")
        if "challenger_sharpe" in ev:
            print(f"   Sharpe champion {ev['champion_sharpe']:.2f} vs challenger {ev['challenger_sharpe']:.2f}; "
                  f"max DD {ev['champion_max_dd']:.1%} vs {ev['challenger_max_dd']:.1%}")
    return 0


def cmd_promote(a) -> int:
    from bot.learn import shadow as S
    evals = dict((i["id"], ev) for i, ev in _shadow_evals(a))
    if a.id not in evals:
        print(f"{a.id} is not an active shadow challenger")
        return 1
    try:
        rec = S.promote(a.id, a.approve, ROOT / "state" / "shadows.json", C.CFG / "selected.yaml", evals[a.id],
                        ROOT / "state" / "selected_backups", ROOT / "state" / "promotions.jsonl")
    except PermissionError as e:
        print(f"REFUSED: {e}")
        return 1
    print(f"promoted {a.id}: {rec['old']} -> {rec['new']}; previous selection saved to {rec['backup']}")
    return 0


def cmd_costs(a) -> int:
    from bot.learn import costs as LC
    if a.apply and not a.by.strip():
        print("REFUSED: --apply needs --by <your name> (decreases make strategies look better; they need a signature)")
        return 1
    u, costs = C.load_universe(), C.load_costs()
    df = LC.collect_fills(ROOT / "runs", u, include_sim=a.include_sim)
    ests = LC.estimate(df, costs)
    be = None
    if a.breakeven:
        panel, meta = P.load_cached_panel(u)
        be = LC.breakeven_cost_multiplier(panel, _selected_or_exit(a), u, C.load_risk(), costs,
                                          C.load_strategy().walkforward["exec_mode"])
    note = ("Using local-simulator fills (circular): for plumbing tests only." if a.include_sim else
            f"{len(df)} broker fills found." if len(df) else "No broker fills yet: nothing to learn from.")
    LC.calibration_md(ests, df, costs, REPORTS / "costs_calibration.md", be, note)
    for e in ests:
        print(f"{e.cls:10s} n={e.n:4d} action={e.action:12s} {e.reason}")
    if a.auto or a.apply:
        applied = LC.apply_estimates(ests, a.by if a.apply else "", only_increases=not a.apply)
        print("applied:", [x.cls for x in applied] or "none",
              "(decreases are only applied with --apply --by NAME)" if a.auto else "")
    return 0


def cmd_select(a) -> int:
    rj = REPORTS / "research_v1.json"
    if not rj.exists():
        print("Run `research` first.")
        return 1
    d = json.loads(rj.read_text(encoding="utf-8"))
    kept = {n: o["params"] for n, o in d["outcomes"].items() if o["verdict"] == "KEEP"}
    path = C.CFG / "selected.yaml"
    path.write_text(yaml.safe_dump({
        "approved_by": a.approve or "", "from_research_tag": d["tag"], "data_id": d.get("data_id", ""),
        "strategies": kept}, sort_keys=False), encoding="utf-8")
    print(f"wrote {path} with {list(kept) or 'NO strategies'}; "
          + (f"approved by {a.approve}" if a.approve else "approved_by is EMPTY: the runner will refuse until set"))
    return 0


def _selected_or_exit(a) -> dict:
    p = C.CFG / "selected.yaml"
    if not p.exists():
        raise SystemExit("config/selected.yaml missing. Run `select` after Gate 1.")
    return yaml.safe_load(p.read_text(encoding="utf-8"))["strategies"]


def cmd_backtest(a) -> int:
    u, risk, costs = C.load_universe(), C.load_risk(), C.load_costs()
    panel, meta, dev, hold, cut, cfg = _dev(P.load_cached_panel(u))
    sel = _selected_or_exit(a)
    tag = meta["tag"]
    res, w, alloc, sleeves = build_portfolio(dev, sel, u, risk, costs, cfg.walkforward["exec_mode"], tag)
    oos_start = dev["close"].index[0] + pd.DateOffset(years=int(cfg.walkforward["min_train_years"]))
    info = portfolio_md(dev, res, w, alloc, sleeves, u, risk, costs, cfg.walkforward["exec_mode"], tag,
                        oos_start, REPORTS / "portfolio_v1.md")
    print(json.dumps({k: v for k, v in info.items() if k != "contrib"}, indent=2, default=float))
    return 0


def cmd_holdout(a) -> int:
    lock = REPORTS / "holdout.json"
    if lock.exists():
        print("The holdout was already evaluated (reports/holdout.json). Re-running it would be re-tuning.")
        return 1
    u, risk, costs = C.load_universe(), C.load_risk(), C.load_costs()
    panel, meta, dev, hold, cut, cfg = _dev(P.load_cached_panel(u))
    sel = _selected_or_exit(a)
    tag = meta["tag"]
    res, *_ = build_portfolio(panel, sel, u, risk, costs, cfg.walkforward["exec_mode"], tag)
    cash = res.cash_ret
    h = res.net.loc[cut:]
    lo, hi = stats.bootstrap_sharpe_ci(h, n_boot=1000, cash=cash)
    base = run_baselines(panel, u, risk, costs, tag, cfg.walkforward["exec_mode"])
    wf = json.loads((REPORTS / "research_v1.json").read_text())["outcomes"]
    wf_sr = [o["oos_sharpe"] for o in wf.values() if o["verdict"] == "KEEP"]
    sr = metrics.sharpe(h, cash)
    e7 = bool(sr > 0 and (not wf_sr or lo <= max(wf_sr)))
    out = {"tag": tag, "start": str(cut.date()), "n_days": len(h), "sharpe": sr, "ci": [lo, hi],
           "max_dd": metrics.max_drawdown(h), "cagr": metrics.cagr(h),
           "baselines": {n: metrics.sharpe(b.net.loc[cut:], cash) for n, b in base.items()},
           "walkforward_sharpes": wf_sr, "E7_pass": e7}
    lock.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    (REPORTS / "holdout.md").write_text(
        f"# Holdout ({tag}), evaluated once\n\nFrom {cut.date()} ({len(h)} days). Net Sharpe {sr:.2f} "
        f"[{lo:.2f}, {hi:.2f}], CAGR {out['cagr']:.1%}, max DD {out['max_dd']:.1%}.\n\n"
        f"E7 (positive, CI consistent with walk-forward {[round(x, 2) for x in wf_sr]}): "
        f"{'PASS' if e7 else 'FAIL'}\n\nBaseline Sharpes: {out['baselines']}\n\nA 2-year holdout has Sharpe SE of "
        f"about 0.7: this is a sanity check against overfitting, not proof.\n", encoding="utf-8")
    print(json.dumps(out, indent=2, default=float))
    return 0


def _broker(a, u, costs):
    from bot.broker.paper_local import PaperLocalBroker
    if a.broker == "alpaca":
        from bot.broker.alpaca_paper import AlpacaPaperBroker
        return AlpacaPaperBroker(), None
    b = PaperLocalBroker(costs, u)
    sp = ROOT / "state" / "paper_local.json"
    if sp.exists():
        b.load(json.loads(sp.read_text(encoding="utf-8")))
    return b, sp


def cmd_paper(a) -> int:
    from bot.run.daily import run_daily
    from bot.run.scheduler import alert
    u, costs = C.load_universe(), C.load_costs()
    panel, meta = P.load_cached_panel(u)
    broker, sp = _broker(a, u, costs)
    # the session we SHOULD have data for now, not the newest bar in the cache: a stale cache must be caught
    from bot.run.scheduler import last_completed_session
    sched = C.load_schedule()
    asof = last_completed_session(pd.Timestamp.now(tz=sched.timezone).to_pydatetime(), sched.timezone,
                                  sched.decision_time)
    if getattr(a, "asof", None):
        asof = pd.Timestamp(a.asof)
    if getattr(a, "refresh", None):
        P.fetch_and_cache(P.choose_provider(a.refresh), u, str(panel["close"].index.min().date()),
                          str(asof.date()), report_path=REPORTS / "data_quality.md")
        panel, meta = P.load_cached_panel(u)
    try:
        res = run_daily(asof, panel, broker, tag=meta["tag"])
    except Exception as e:  # noqa: BLE001
        alert(f"daily run failed: {type(e).__name__}: {e}")
        raise
    if sp is not None:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(broker.dump()), encoding="utf-8")
    print(f"{res.run_id}: {res.status}, ladder {res.ladder}, equity {res.equity}, notes {res.notes}")
    if res.status in ("error", "halted") or res.mismatches:
        alert(f"{res.run_id} {res.status} {res.notes}")
    return 0


def cmd_nightly(a) -> int:
    rc = cmd_paper(a)
    from bot.monitor.weekly_review import write_weekly
    if pd.Timestamp.today().weekday() == 4 or a.weekly:
        dj = ROOT / "state" / "drift.json"
        note = ""
        if dj.exists():
            d = json.loads(dj.read_text())
            note = f"- status {d['action']}: {'; '.join(d['reasons']) or 'no concerns'}"
        print("weekly:", write_weekly(ROOT / "runs", REPORTS, drift_note=note))
    try:  # unattended learning step: only ever makes cost estimates MORE conservative
        a.auto, a.apply, a.by, a.breakeven, a.include_sim = True, False, "", False, False
        cmd_costs(a)
    except Exception as e:  # noqa: BLE001
        print(f"cost calibration skipped: {e}")
    _drift_step()
    cmd_gate(a)
    return rc


def _drift_step() -> None:
    """Daily drift check: realised Sharpe/vol/tracking error vs what research expected. Persistent shortfall
    switches the book to cash (state/CASH_MODE, removed only by a person) and alerts."""
    from bot.monitor.drift import apply_action, evaluate_runs
    from bot.monitor.weekly_review import load_runs
    from bot.run.scheduler import alert
    try:
        runs = load_runs(ROOT / "runs")
        rj = REPORTS / "research_v1.json"
        exp = [o["oos_sharpe"] for o in json.loads(rj.read_text())["outcomes"].values() if o["verdict"] == "KEEP"] \
            if rj.exists() else []
        expected = float(sum(exp) / len(exp)) if exp else 0.5
        replay, _ = _replay_for_runs(runs)
        status = evaluate_runs(runs, replay, expected, C.load_risk().target_vol_annual)
        apply_action(status, ROOT / "state", alert)
        print("drift:", status["action"], status["reasons"])
    except Exception as e:  # noqa: BLE001
        print(f"drift check skipped: {type(e).__name__}: {e}")


def cmd_gate(a) -> int:
    from bot.monitor.gate import combined_checklist, implementation_gate
    from bot.monitor.weekly_review import load_runs
    runs = load_runs(ROOT / "runs")
    dp = REPORTS / "drills.json"
    drills = json.loads(dp.read_text()) if dp.exists() else None
    paper_days = getattr(a, "paper_days", 60)
    replay, modelled = _replay_for_runs(runs)
    impl = implementation_gate(runs, paper_days, replay, modelled, C.load_risk().target_vol_annual, drills)
    out = combined_checklist(REPORTS, impl)
    REPORTS.mkdir(exist_ok=True)
    _write_benchmarks(runs)
    (REPORTS / "gate_status.json").write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    for c in out["edge"] + out["implementation"]:
        print(f"{c['id']:3s} {'PASS' if c['pass'] else 'FAIL'}  {c['desc']}  [{c['value']}]")
    print("ALL PASS" if out["all_pass"] else "Gate not passed (see above; SYNTHETIC research can never pass).")
    return 0


def _write_benchmarks(runs) -> None:
    """Benchmark equity lines (SPY, 60/40) aligned to the paper-run dates, for the dashboard."""
    if runs.empty:
        return
    try:
        panel, _ = P.load_cached_panel()
    except SystemExit:
        return
    cap = float(runs["equity"].iloc[0])
    c = panel["close"].reindex(runs.index).ffill()
    out = {}
    if "SPY" in c:
        out["SPY"] = (cap * c["SPY"] / c["SPY"].iloc[0]).round(2).tolist()
    if {"SPY", "IEF"} <= set(c.columns):
        r = 0.6 * c["SPY"].pct_change().fillna(0) + 0.4 * c["IEF"].pct_change().fillna(0)
        out["60/40"] = (cap * (1 + r).cumprod()).round(2).tolist()
    (REPORTS / "benchmarks.json").write_text(json.dumps(out), encoding="utf-8")


def _replay_for_runs(runs):
    """Backtest replay of the selected book over the paper-run dates (empty book at the first date)."""
    if runs.empty or not (C.CFG / "selected.yaml").exists():
        return None, None
    try:
        from bot.backtest.engine import rebalance_dates, run_backtest
        from bot.portfolio.construct import book_rebalance
        from bot.run.daily import compute_targets
        u, risk, costs = C.load_universe(), C.load_risk(), C.load_costs()
        panel, meta = P.load_cached_panel(u)
        sel = yaml.safe_load((C.CFG / "selected.yaml").read_text(encoding="utf-8"))["strategies"]
        tg, _ = compute_targets(panel["close"], sel, u, risk)
        freq = book_rebalance(sel, make_registry(C.load_strategy()))
        res = run_backtest({**panel, "volume": None}, tg, costs=costs, risk=risk, universe=u,
                           rebalance=rebalance_dates(tg.index, freq), start=runs.index[0])
        equity_prev = costs.start_capital_usd * (1 + res.net).cumprod().shift(1).fillna(1.0)
        modelled = float((res.cost.reindex(runs.index).fillna(0.0) * equity_prev.reindex(runs.index)).sum())
        return res.net, modelled
    except SystemExit:
        return None, None


def cmd_drills(a) -> int:
    from bot.monitor import drills
    res = {"kill_switch": drills.drill_kill_switch(), "drawdown_halt": drills.drill_drawdown_halt(),
           "crash_recovery": drills.drill_crash_recovery()}
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "drills.json").write_text(json.dumps(res), encoding="utf-8")
    print(res)
    return 0 if all(res.values()) else 1


def cmd_kill(a) -> int:
    KillSwitch().engage(a.reason or "CLI", by="cli")
    print("kill switch ENGAGED. Release with: python -m bot.cli release --by <name>")
    return 0


def cmd_release(a) -> int:
    KillSwitch().release(a.by)
    print("kill switch released")
    return 0


def cmd_dash(a) -> int:
    import uvicorn

    from bot.dashboard.api import create_app, ensure_token
    ensure_token(ROOT)
    print(f"dashboard on http://127.0.0.1:{a.port}  (token in state/dash_token)")
    uvicorn.run(create_app(ROOT), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


def cmd_check(a) -> int:
    from bot.config import check_risk_hash
    print("risk hash ok:", check_risk_hash())
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    p = argparse.ArgumentParser(prog="bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("data")
    d.add_argument("--source", default="csv", choices=["csv", "yahoo", "alpaca", "synthetic"])
    d.add_argument("--start", default="2003-01-01")
    d.add_argument("--end", default=str(pd.Timestamp.today().date()))
    d.add_argument("--seed", type=int, default=0)
    d.add_argument("--null", action="store_true", help="synthetic: null world (no planted edge)")
    d.set_defaults(fn=cmd_data)
    r = sub.add_parser("research")
    r.add_argument("--placebo", type=int, default=500)
    r.add_argument("--boot", type=int, default=1000)
    r.add_argument("--only", default="")
    r.set_defaults(fn=cmd_research)
    s = sub.add_parser("select")
    s.add_argument("--approve", default="", help="your name: signs off Gate 1 for the KEEP strategies")
    s.set_defaults(fn=cmd_select)
    sub.add_parser("backtest").set_defaults(fn=cmd_backtest)
    sub.add_parser("budget").set_defaults(fn=cmd_budget)
    pr = sub.add_parser("propose")
    pr.add_argument("file", help='JSON: {"strategy": "trend", "grid": {"sma": [120]}, "rationale": "..."}')
    pr.set_defaults(fn=cmd_propose)
    ln = sub.add_parser("learn")
    ln.add_argument("--placebo", type=int, default=200)
    ln.add_argument("--boot", type=int, default=500)
    ln.set_defaults(fn=cmd_learn)
    sub.add_parser("shadow").set_defaults(fn=cmd_shadow)
    pm = sub.add_parser("promote")
    pm.add_argument("id")
    pm.add_argument("--approve", required=True, help="your name")
    pm.set_defaults(fn=cmd_promote)
    co = sub.add_parser("costs")
    co.add_argument("--auto", action="store_true", help="apply cost INCREASES only (safe for unattended use)")
    co.add_argument("--apply", action="store_true", help="apply increases and decreases (needs --by)")
    co.add_argument("--by", default="")
    co.add_argument("--breakeven", action="store_true", help="also compute the break-even cost multiplier")
    co.add_argument("--include-sim", action="store_true", dest="include_sim")
    co.set_defaults(fn=cmd_costs)
    sub.add_parser("holdout").set_defaults(fn=cmd_holdout)
    for name, fn in (("paper", cmd_paper), ("nightly", cmd_nightly)):
        q = sub.add_parser(name)
        q.add_argument("--broker", default="local", choices=["local", "alpaca"])
        q.add_argument("--weekly", action="store_true")
        q.add_argument("--refresh", default="", choices=["", "csv", "yahoo", "alpaca"],
                       help="re-fetch data from this source before running (stale data is then blocked)")
        q.add_argument("--asof", default="", help="run for this session date instead of the latest one (demos)")
        q.set_defaults(fn=fn)
    g = sub.add_parser("gate")
    g.add_argument("--paper-days", type=int, default=60, dest="paper_days")
    g.set_defaults(fn=cmd_gate)
    sub.add_parser("drills").set_defaults(fn=cmd_drills)
    k = sub.add_parser("kill")
    k.add_argument("--reason", default="")
    k.set_defaults(fn=cmd_kill)
    rl = sub.add_parser("release")
    rl.add_argument("--by", required=True)
    rl.set_defaults(fn=cmd_release)
    ds = sub.add_parser("dash")
    ds.add_argument("--port", type=int, default=8765)
    ds.set_defaults(fn=cmd_dash)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
