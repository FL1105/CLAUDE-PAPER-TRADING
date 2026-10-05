"""polybot command-line interface.

Assumption: every command is read-only against public endpoints or local SQLite.
Failure mode guarded against: commands from later build phases silently doing
nothing. Unbuilt commands exit non-zero with the phase that will implement them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

from polybot.config import config_hash, load_config

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


@app.command()
def backfill(days: int = 90) -> None:
    """Historical ingestion (resumable)."""
    _not_yet("backfill", 3)


@app.command()
def select(epoch_start: str = typer.Option(..., "--epoch-start")) -> None:
    """Run wallet selection for an epoch."""
    _not_yet("select", 3)


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
