"""polybot command-line interface.

Assumption: every command is read-only against public endpoints or local SQLite.
Failure mode guarded against: commands from later build phases silently doing
nothing. Unbuilt commands exit non-zero with the phase that will implement them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from polybot.config import Config, config_hash, load_config

if TYPE_CHECKING:
    import pandas as pd

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
    (out / "probe_report.json").write_text(json.dumps(report.to_json(), indent=1, default=str))
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
                ws = [w.strip() for w in wallets_file.read_text().splitlines() if w.strip()]
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
def backtest(days: int = 90) -> None:
    """Walk-forward OOS backtest incl. placebo and stress tests."""
    _not_yet("backtest", 5)


@app.command()
def paper() -> None:
    """Forward polling loop (paper signals/fills/settlements)."""
    _not_yet("paper", 6)


@app.command()
def settle() -> None:
    """Resolve open paper positions from Gamma."""
    _not_yet("settle", 6)


@app.command()
def report() -> None:
    """Write out/report.json, trades.csv, equity.csv."""
    _not_yet("report", 6)


@app.command()
def simulate(
    world: str = typer.Option("null", "--world", help="null | skilled"),
    seeds: int = typer.Option(300, "--seeds"),
    latency_ratio: float = typer.Option(
        0.1, "--latency-ratio", help="used from Phase 4/5 (execution + gates)"
    ),
    price_model: str | None = typer.Option(
        None, "--price-model", help="calibrated | additive (default: config)"
    ),
    demo_seeds: int = typer.Option(
        50, "--demo-seeds", help="seeds for the naive-vs-BH selection demo"
    ),
    config: Path | None = ConfigOpt,
) -> None:
    """Synthetic-world validation: p-value calibration + selection-bias demo.

    Phase 2 scope. Full pipeline + gates + latency (false-promotion rate, power)
    are added in Phase 5.
    """
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
    result["note"] = (
        f"latency_ratio={latency_ratio} not used until Phase 4/5; "
        "gates/false-promotion rate not yet implemented"
    )
    text = json.dumps(result, indent=1, sort_keys=True)
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    pm = price_model or cfg.synthetic.price_model
    (out / f"simulate_{world}_{pm}.json").write_text(text + "\n")
    typer.echo(text)


@app.command()
def gates(explain: bool = typer.Option(False, "--explain")) -> None:
    """Print every gate: observed vs threshold, plain-language explanation."""
    _not_yet("gates", 5)


if __name__ == "__main__":
    app()
