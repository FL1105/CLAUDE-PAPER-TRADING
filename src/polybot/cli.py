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
    world: str = typer.Option("null", "--world"),
    seeds: int = typer.Option(300, "--seeds"),
    latency_ratio: float = typer.Option(0.1, "--latency-ratio"),
) -> None:
    """Run synthetic-world validation."""
    _not_yet("simulate", 2)


@app.command()
def gates(explain: bool = typer.Option(False, "--explain")) -> None:
    """Print every gate: observed vs threshold, plain-language explanation."""
    _not_yet("gates", 5)


if __name__ == "__main__":
    app()
