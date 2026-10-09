"""Command line interface: python -m bot.cli <command>. See docs/RUNBOOK.md."""
from __future__ import annotations

import argparse
import json
import sys

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
    res = research(dev, u, risk, costs, reg, cfg.walkforward, exp, tag, did, n_placebo=a.placebo,
                   n_boot=a.boot, cover_min=2.0, only=a.only.split(",") if a.only else None)
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
    asof = panel["close"].dropna(how="all").index.max()
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
        print("weekly:", write_weekly(ROOT / "runs", REPORTS))
    cmd_gate(a)
    return rc


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
    d.add_argument("--source", default="csv", choices=["csv", "alpaca", "synthetic"])
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
    sub.add_parser("holdout").set_defaults(fn=cmd_holdout)
    for name, fn in (("paper", cmd_paper), ("nightly", cmd_nightly)):
        q = sub.add_parser(name)
        q.add_argument("--broker", default="local", choices=["local", "alpaca"])
        q.add_argument("--weekly", action="store_true")
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
