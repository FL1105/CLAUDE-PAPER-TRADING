"""polybot command-line interface.

Assumption: every command is read-only against public endpoints or local SQLite.
Failure mode guarded against: commands from later build phases silently doing
nothing. Unbuilt commands exit non-zero with the phase that will implement them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from polybot.config import Config, config_hash, load_config

if TYPE_CHECKING:
    import pandas as pd

    from polybot.data.client import HttpClient
    from polybot.data.store import Store
    from polybot.forward import ForwardLoop

# Windows writes redirected output (e.g. a scheduled task's log file) in a legacy code
# page that cannot encode characters like "->" arrows; always emit UTF-8 instead.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Polymarket wallet-edge PAPER bot. No real-money code paths.",
)

ROOT = Path(__file__).resolve().parents[2]
ConfigOpt = typer.Option(None, "--config", "-c", help="Path to config YAML.")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='{"ts":"%(asctime)s","lvl":"%(levelname)s","log":"%(name)s","msg":"%(message)s"}',
    )


def _not_yet(cmd: str, phase: int) -> None:
    typer.echo(f"`polybot {cmd}` is not implemented yet (build phase {phase}).", err=True)
    raise typer.Exit(code=2)


@app.command()
def probe(
    config: Path | None = ConfigOpt,
    fixtures_dir: Path = typer.Option(ROOT / "tests" / "fixtures" / "live"),
    assumptions: Path = typer.Option(ROOT / "docs" / "ASSUMPTIONS.md"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Hit each endpoint once; save fixtures; write findings into docs/ASSUMPTIONS.md."""
    from polybot.data.client import HttpClient, PolymarketClient
    from polybot.data.probe import Prober, render_markdown, write_assumptions

    _setup_logging(verbose)
    cfg = load_config(config)
    with HttpClient(cfg.api, seed=cfg.seed) as http:
        report = Prober(PolymarketClient(http), fixtures_dir).run()
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "probe_report.json").write_text(
        json.dumps(report.to_json(), indent=1, default=str), encoding="utf-8"
    )
    failed = [s.name for s in report.steps if not s.ok]
    drift = [k for k, v in report.drift.items() if v.startswith("DRIFT")]
    if not report.steps or len(failed) == len(report.steps):
        # Never record an all-failed probe as findings: nothing was verified.
        typer.echo(
            f"No endpoint reachable ({failed}). Check network / VPN / regional "
            "access. docs/ASSUMPTIONS.md left unchanged.",
            err=True,
        )
        raise typer.Exit(code=1)
    md = render_markdown(report)
    write_assumptions(assumptions, md)
    typer.echo(md)
    typer.echo(f"\nsteps={len(report.steps)} failed={failed} drift={drift}")
    if drift:
        typer.echo("SCHEMA DRIFT: fix data/schemas.py before building on this data.", err=True)
        raise typer.Exit(code=3)


@app.command()
def doctor(
    config: Path | None = ConfigOpt,
    offline: bool = typer.Option(False, "--offline", help="skip the network checks"),
) -> None:
    """Check this machine is ready (Python, packages, network, database, forward run)."""
    from polybot.doctor import FAIL, render, run

    checks = run(ROOT, config, offline=offline)
    typer.echo(render(checks))
    if any(c.status == FAIL for c in checks):
        raise typer.Exit(code=1)


@app.command("config-hash")
def config_hash_cmd(config: Path | None = ConfigOpt) -> None:
    """Validate the config and print its hash."""
    cfg = load_config(config)
    typer.echo(f"config_version={cfg.config_version} hash={config_hash(cfg)}")


def _parse_date(s: str) -> int:
    from datetime import UTC, datetime

    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _now() -> int:
    import time

    return int(time.time())


@app.command()
def backfill(
    days: int = typer.Option(90, "--days"),
    markets_only: bool = typer.Option(
        False, "--markets-only", help="only look up / refresh markets for stored trades"
    ),
    wallets_file: Path | None = typer.Option(
        None, "--wallets-file", help="also pull per-wallet history (one address per line)"
    ),
    config: Path | None = ConfigOpt,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Historical ingestion from the public API (resumable, idempotent)."""
    from polybot.data.client import HttpClient, PolymarketClient, ResponseCache
    from polybot.data.ingest import Ingestor
    from polybot.data.store import Store

    _setup_logging(verbose)
    cfg = load_config(config)
    end = _now()
    start = end - days * 86_400
    with (
        HttpClient(cfg.api, seed=cfg.seed, cache=ResponseCache(ROOT / cfg.api.cache_dir)) as h,
        Store(ROOT / cfg.storage.db_path) as st,
    ):
        ing = Ingestor(PolymarketClient(h), st, cfg)
        out: dict[str, object] = {}
        if not markets_only:
            r = ing.backfill_trades(start, end)
            out["trades"] = r.__dict__
            if r.stopped_because == "offset_cap":
                typer.echo(
                    f"NOTE: the global feed only reached ts={r.oldest_ts}, not {start}. "
                    "Use --wallets-file or `polybot history import` for deeper history.",
                    err=True,
                )
            if wallets_file is not None:
                ws = [
                    w.strip()
                    for w in wallets_file.read_text(encoding="utf-8").splitlines()
                    if w.strip()
                ]
                out["wallets"] = {w: ing.backfill_wallet(w, start, end).__dict__ for w in ws}
        out["markets"] = ing.refresh_markets().__dict__
        out["counts"] = {
            t: st.count(t) for t in ("trades", "markets", "tokens", "schema_drift", "anomalies")
        }
        out["anomalies"] = st.anomaly_counts()
    typer.echo(json.dumps(out, indent=1, default=str))


history_app = typer.Typer(help="Import bulk historical trades (CSV/Parquet/remote via DuckDB).")
app.add_typer(history_app, name="history")


@history_app.command("inspect")
def history_inspect(
    path: str = typer.Argument(..., help="file path, glob, URL or hf:// path"),
    mapping: Path | None = typer.Option(None, "--mapping", help="SQL file with {src}"),
) -> None:
    """Print columns, types and sample rows (do this before importing)."""
    from polybot.data.history import inspect, read_mapping

    typer.echo(json.dumps(inspect(path, read_mapping(mapping)), indent=1, default=str))


@history_app.command("import")
def history_import(
    path: str = typer.Argument(...),
    name: str = typer.Option(..., "--name", help="label for this source, e.g. hf-sii-2026"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD (inclusive)"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD (exclusive)"),
    mapping: Path | None = typer.Option(None, "--mapping", help="SQL file with {src}"),
    config: Path | None = ConfigOpt,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Import trades in [start, end) into the local database."""
    from polybot.data.history import import_trades, read_mapping
    from polybot.data.store import Store

    _setup_logging(verbose)
    cfg = load_config(config)
    with Store(ROOT / cfg.storage.db_path) as st:
        r = import_trades(
            st,
            path,
            name=name,
            start_ts=_parse_date(start),
            end_ts=_parse_date(end),
            mapping_sql=read_mapping(mapping),
            batch=cfg.ingest.history_import_batch,
        )
        typer.echo(json.dumps({**r.__dict__, "drift_total": st.count("schema_drift")}, indent=1))
    typer.echo("Next: `polybot backfill --markets-only` to attach markets and resolutions.")


def _frames(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    from polybot.data.store import Store

    with Store(ROOT / cfg.storage.db_path) as st:
        return st.trades_frame(), st.markets_frame()


@app.command()
def select(
    epoch_start: str = typer.Option(..., "--epoch-start", help="YYYY-MM-DD"),
    config: Path | None = ConfigOpt,
) -> None:
    """Run wallet selection for one epoch from local data; print table + diagnostics."""
    from polybot.selection.walkforward import DataView, Epoch, select_wallets

    cfg = load_config(config)
    trades, markets = _frames(cfg)
    s = _parse_date(epoch_start)
    ep = Epoch(0, s, s + cfg.walkforward.epoch_days * 86_400)
    sel = select_wallets(DataView(trades, markets, s), ep, cfg, cfg.seed)
    top = sel.edges.sort_values("p_value").head(25) if sel.edges is not None else None
    typer.echo(
        json.dumps(
            {
                "epoch_start": s,
                "selected": list(sel.wallets),
                "naive_top_pnl": list(sel.naive),
                "diagnostics": sel.diagnostics,
            },
            indent=1,
            default=str,
        )
    )
    if top is not None and len(top):
        typer.echo(top.to_string(index=False))


@app.command()
def walkforward(
    days: int = typer.Option(90, "--days", help="evaluation span (after the trailing window)"),
    config: Path | None = ConfigOpt,
) -> None:
    """Walk-forward OOS evaluation on local data (gross, at wallet prices; Phase 3)."""
    from polybot.selection.walkforward import walk_forward

    cfg = load_config(config)
    trades, markets = _frames(cfg)
    end = int(trades["ts"].max()) + 1 if len(trades) else _now()
    start = end - days * 86_400
    r = walk_forward(trades, markets, cfg, start, end, seed=cfg.seed)
    for e in r.epochs:
        e.pop("diagnostics")
    typer.echo(
        json.dumps(
            {"config_hash": config_hash(cfg), "epochs": r.epochs, "aggregate": r.aggregate},
            indent=1,
            default=str,
        )
    )


@app.command()
def backtest(
    days: int = typer.Option(90, "--days", help="evaluation span after the trailing window"),
    config: Path | None = ConfigOpt,
) -> None:
    """Walk-forward OOS backtest on local history: tape-priced fills, stress, placebo, gates.

    Backtest evidence can reach tier Common (S0) at most; gate 1 needs live_forward."""
    from polybot.backtest import run_backtest
    from polybot.data.store import Store
    from polybot.execution.replay import tape_price_fn
    from polybot.gates.ladder import kill_switches, ladder
    from polybot.selection.walkforward import Epoch, leakage_selfcheck

    cfg = load_config(config)
    h = config_hash(cfg)
    trades, markets = _frames(cfg)
    if trades.empty:
        typer.echo(
            "No trades in the database. Run `polybot backfill` / `history import`.", err=True
        )
        raise typer.Exit(code=1)
    end = int(trades["ts"].max()) + 1
    start = end - days * 86_400
    ep0 = Epoch(0, start, start + cfg.walkforward.epoch_days * 86_400)
    leaks = leakage_selfcheck(trades, markets, cfg, ep0, cfg.seed)
    with Store(ROOT / cfg.storage.db_path) as st:
        drift = st.drift_count_since(_now() - cfg.gates.schema_drift_lookback_days * 86_400)
    r = run_backtest(
        trades,
        markets,
        cfg,
        start_ts=start,
        end_ts=end,
        price_at=tape_price_fn(trades, cfg.execution.tape_min_window_s),
        mode="backtest",
        seed=cfg.seed,
        config_hash=h,
        leakage_failures=leaks,
        recent_drift_errors=drift,
    )
    lad = ladder(r.evidence, cfg)
    out = {
        "config_hash": h,
        "evidence_mode": "backtest",
        "start_ts": start,
        "end_ts": end,
        "ladder": lad.as_dict(),
        "kill_switches": [k.__dict__ for k in kill_switches(r.evidence, cfg)],
        "replay_summary": r.extras["replay_summary"],
        "stress": r.extras["stress"],
        "walkforward": r.extras["walkforward_aggregate"],
        "placebo": {
            "n": len(r.evidence.placebo_rois or ()),
            "rois": list(r.evidence.placebo_rois or ()),
        },
        "note": "Backtest evidence is capped at tier Common. Fills use the trade tape plus a "
        "SYNTHETIC spread/depth assumption (docs/ASSUMPTIONS.md section 7).",
    }
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "backtest.json").write_text(
        json.dumps(out, indent=1, default=str), encoding="utf-8"
    )
    typer.echo(f"tier={lad.tier} next={lad.next_tier} promoted={r.promoted}")
    for g in r.gates:
        typer.echo(f"  {'PASS' if g.passed else 'fail'}  {g.name:28s} {g.observed}")
    typer.echo("Wrote out/backtest.json. `polybot gates --explain` for plain language.")


@app.command()
def resume(
    note: str = typer.Option(..., "--note", help="what you reviewed and why it is OK"),
    config: Path | None = ConfigOpt,
) -> None:
    """Clear a kill-switch halt after manual review (the note is logged)."""
    from polybot.data.store import Store

    cfg = load_config(config)
    with Store(ROOT / cfg.storage.db_path) as st:
        h = st.get_halt()
        if h is None:
            typer.echo("Not halted.")
            return
        st.clear_halt(f"cleared '{h}': {note}")
    typer.echo(f"Halt '{h}' cleared. Logged.")


def _forward(cfg: Config) -> tuple[ForwardLoop, HttpClient, Store]:
    from polybot.data.client import HttpClient, PolymarketClient
    from polybot.data.store import Store
    from polybot.forward import ForwardLoop

    http = HttpClient(cfg.api, seed=cfg.seed)
    st = Store(ROOT / cfg.storage.db_path)
    return ForwardLoop(cfg, st, PolymarketClient(http), ROOT / "out"), http, st


@app.command()
def paper(
    iterations: int | None = typer.Option(
        None, "--iterations", help="stop after N iterations (default: run forever)"
    ),
    once: bool = typer.Option(False, "--once", help="run exactly one iteration"),
    config: Path | None = ConfigOpt,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Forward paper-trading loop (Ctrl-C to stop; safe to restart any time)."""
    _setup_logging(verbose)
    loop, http, st = _forward(load_config(config))
    try:
        loop.run(max_iterations=1 if once else iterations)
    finally:
        http.close()
        st.close()


@app.command()
def settle(config: Path | None = ConfigOpt, verbose: bool = typer.Option(False, "-v")) -> None:
    """Resolve open paper positions from Gamma (also runs inside `paper`)."""
    _setup_logging(verbose)
    loop, http, st = _forward(load_config(config))
    try:
        n = loop.settle()
        typer.echo(f"settled {n} positions; open: {len(loop.executor.state.open)}")
    finally:
        http.close()
        st.close()


@app.command()
def report(config: Path | None = ConfigOpt) -> None:
    """Write out/report.json, out/trades.csv, out/equity.csv (open dashboard.html to view)."""
    from polybot.data.store import Store
    from polybot.report.build import write_outputs

    cfg = load_config(config)
    with Store(ROOT / cfg.storage.db_path) as st:
        rep = write_outputs(st, cfg, ROOT / "out", backtest_path=ROOT / "out" / "backtest.json")
    lad = rep["ladder"]
    s = rep["summary"]
    typer.echo(
        f"tier={lad['tier']} next={lad['next_tier']} settled={s['settled_trades']} "
        f"roi={s['roi_net']} halted={rep['halted']}"
    )
    typer.echo("Blocking: " + ", ".join(b["name"] for b in lad["blocking_next"]))
    typer.echo(rep["power"]["statement"])
    typer.echo("Wrote out/report.json, out/trades.csv, out/equity.csv. Open dashboard.html.")


@app.command()
def simulate(
    world: str = typer.Option("null", "--world", help="null | skilled"),
    seeds: int = typer.Option(300, "--seeds"),
    latency_ratio: float = typer.Option(
        0.1, "--latency-ratio", help="latency / information half-life (d/h)"
    ),
    price_model: str | None = typer.Option(
        None, "--price-model", help="calibrated | additive (default: config)"
    ),
    acceptance_run: bool = typer.Option(
        True, "--acceptance/--no-acceptance", help="full pipeline + all gates (simulation mode)"
    ),
    demo_seeds: int = typer.Option(
        50, "--demo-seeds", help="seeds for the naive-vs-BH selection demo"
    ),
    config: Path | None = ConfigOpt,
) -> None:
    """Synthetic validation: p-value calibration, selection-bias demo, and the full
    pipeline + all paper gates (false-promotion rate / power) at d/h = latency_ratio."""
    from polybot.data.synthetic import World
    from polybot.experiments import null_calibration, selection_bias_over_seeds

    if world not in ("null", "skilled"):
        raise typer.BadParameter("world must be null or skilled")
    cfg = load_config(config)
    seed_list = [cfg.seed + i for i in range(seeds)]
    result: dict[str, object] = {"config_hash": config_hash(cfg), "world": world}
    if world == "null":
        result["calibration"] = null_calibration(cfg, seed_list, price_model)
    wld: World = "null" if world == "null" else "skilled"
    result["selection_bias"] = selection_bias_over_seeds(
        cfg, seed_list[:demo_seeds], wld, price_model
    )
    if acceptance_run:
        import os

        from polybot.experiments import acceptance

        acc = acceptance(
            cfg, seed_list, wld, latency_ratio, price_model, workers=os.cpu_count() or 1
        )
        acc.pop("runs")
        result["acceptance"] = acc
    text = json.dumps(result, indent=1, sort_keys=True)
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    pm = price_model or cfg.synthetic.price_model
    (out / f"simulate_{world}_{pm}.json").write_text(text + "\n", encoding="utf-8")
    typer.echo(text)


@app.command()
def gates(
    explain: bool = typer.Option(False, "--explain"),
    source: Path = typer.Option(
        ROOT / "out" / "backtest.json", "--from", help="report.json or backtest.json"
    ),
) -> None:
    """Print every gate: observed vs threshold, pass/fail, plain-language explanation."""
    from polybot.gates.gates import EXPLAIN

    if not source.exists():
        typer.echo(
            f"{source} not found. Run `polybot backtest` or `polybot report` first.", err=True
        )
        raise typer.Exit(code=1)
    data = json.loads(source.read_text(encoding="utf-8"))
    lad = data["ladder"]
    typer.echo(
        f"Evidence: {data.get('evidence_mode')}   tier: {lad['tier']}   next: {lad['next_tier']}"
    )
    for g in lad["gates"]:
        typer.echo(f"\n[{'PASS' if g['passed'] else 'FAIL'}] {g['name']}")
        typer.echo(f"   observed : {g['observed']}")
        typer.echo(f"   threshold: {g['threshold']}")
        if explain:
            typer.echo(f"   why      : {EXPLAIN.get(g['name'], '')}")
            if g.get("detail"):
                typer.echo(f"   detail   : {g['detail']}")
    typer.echo("\nBlocking the next tier: " + ", ".join(b["name"] for b in lad["blocking_next"]))
    typer.echo("\nManual real-money checklist (cannot be automated):")
    for c in lad["manual_checklist"]:
        typer.echo(f"  [ ] {c['item']}")


if __name__ == "__main__":
    app()


fleet_app = typer.Typer(help="Run several candidate configs side by side (paper only).")
app.add_typer(fleet_app, name="fleet")
CandDirOpt = typer.Option(ROOT / "config" / "candidates", "--candidates", help="candidate YAML dir")
FLEET_DIR = ROOT / "out" / "fleet"


@fleet_app.command("init")
def fleet_init(candidates: Path = CandDirOpt, config: Path | None = ConfigOpt) -> None:
    """Create one database per candidate, seeded from the backfilled history."""
    from polybot.fleet import load_candidates, seed_database

    cfg = load_config(config)
    for c in load_candidates(candidates, ROOT, FLEET_DIR):
        seeded = seed_database(c, ROOT / cfg.storage.db_path)
        typer.echo(
            f"{c.name}: {'seeded from ' + cfg.storage.db_path if seeded else 'exists/skipped'}"
        )


@fleet_app.command("run")
def fleet_run(
    candidates: Path = CandDirOpt,
    iterations: int | None = typer.Option(None, "--iterations"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Forward-paper every candidate in one process (Ctrl-C safe; restart any time)."""
    from polybot.data.client import HttpClient, PolymarketClient
    from polybot.fleet import load_candidates, run_fleet, seed_database

    _setup_logging(verbose)
    cands = load_candidates(candidates, ROOT, FLEET_DIR)
    base = load_config(None)
    for c in cands:
        seed_database(c, ROOT / base.storage.db_path)
    with HttpClient(base.api, seed=base.seed) as http:
        run_fleet(cands, PolymarketClient(http), max_iterations=iterations)


@fleet_app.command("report")
def fleet_report(candidates: Path = CandDirOpt) -> None:
    """Write out/fleet/fleet.json and out/fleet/fleet.html (open it in a browser)."""
    from polybot.fleet import load_candidates, write_fleet
    from polybot.fleet_view import write_html

    cands = load_candidates(candidates, ROOT, FLEET_DIR)
    data = write_fleet(cands)
    write_html(data, FLEET_DIR / "fleet.html")
    for r in data["candidates"]:
        i = r["income_30d"]
        rng = "n/a" if i["lo"] is None else f"{i['lo']:+.0f}..{i['hi']:+.0f}"
        typer.echo(
            f"{r['name']}: tier={r['tier']} trades={r['summary']['settled_trades']} "
            f"pnl={r['summary']['pnl']:+.2f} 30d-range={rng}"
        )
    typer.echo(f"Wrote {FLEET_DIR / 'fleet.html'}")
