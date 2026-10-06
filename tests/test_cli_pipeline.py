"""End-to-end: synthetic world -> CSV -> `history import` -> SQLite -> select / walkforward."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

from polybot.cli import app
from polybot.config import DEFAULT_CONFIG_PATH, load_config
from polybot.data.schemas import MarketRecord, ResolutionStatus, TokenRecord
from polybot.data.store import Store
from polybot.data.synthetic import DAY, T0, generate_world


def test_pipeline_from_csv_import(tmp_path: Path) -> None:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["storage"]["db_path"] = str(tmp_path / "db.sqlite")
    raw["synthetic"].update({"n_markets": 800, "n_wallets": 400, "calibration_mc_sims": 200})
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(cfg_path)

    w = generate_world(cfg.synthetic, 3, "null")
    t = w.trades
    api = t.assign(
        proxyWallet="0x" + t["wallet"].str[1:].str.zfill(40),
        asset="tok" + t["market"].astype(str) + "_" + t["outcome"].astype(str),
        conditionId="0xm" + t["market"].astype(str),
        size=t["shares"],
        timestamp=t["ts"],
        outcomeIndex=t["outcome"],
        transactionHash="0x" + t.index.astype(str),
    )[
        [
            "proxyWallet",
            "side",
            "asset",
            "conditionId",
            "size",
            "price",
            "timestamp",
            "outcomeIndex",
            "transactionHash",
        ]
    ]
    csv = tmp_path / "hist.csv"
    api.to_csv(csv, index=False)

    with Store(cfg.storage.db_path) as st:
        st.upsert_markets(
            (
                MarketRecord(
                    condition_id=f"0xm{r.market}",
                    question="synthetic",
                    end_ts=r.end_ts,
                    resolved_ts=r.end_ts,
                    resolution_status=ResolutionStatus.RESOLVED,
                    winning_outcome_index=int(r.winning_outcome),
                    settlement_prices=(1.0, 0.0) if r.winning_outcome == 0 else (0.0, 1.0),
                    category=None,
                    neg_risk=False,
                ),
                [
                    TokenRecord(
                        token_id=f"tok{r.market}_{i}",
                        condition_id=f"0xm{r.market}",
                        outcome_index=i,
                        outcome_label=lbl,
                    )
                    for i, lbl in enumerate(["Yes", "No"])
                ],
            )
            for r in w.markets.itertuples()
        )

    run = CliRunner()

    def day(d: int) -> str:
        return datetime.fromtimestamp(T0 + d * DAY, tz=UTC).strftime("%Y-%m-%d")

    r = run.invoke(
        app,
        [
            "history",
            "import",
            str(csv),
            "--name",
            "synth",
            "--start",
            day(0),
            "--end",
            day(200),
            "-c",
            str(cfg_path),
        ],
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output.split("\nNext:")[0])["inserted"] == len(t)

    r = run.invoke(app, ["select", "--epoch-start", day(90), "-c", str(cfg_path)])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output[: r.output.index("\n}\n") + 2])
    assert out["diagnostics"]["markets_in_window"] > 0 and "selected" in out

    r = run.invoke(app, ["walkforward", "--days", "56", "-c", str(cfg_path)])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    assert res["aggregate"]["n_epochs"] == 4

    r = run.invoke(app, ["backtest", "--days", "56", "-c", str(cfg_path)])
    assert r.exit_code == 0, r.output
    assert "tier=" in r.output and (tmp_path / "x").parent.exists()
    r = run.invoke(app, ["gates", "--explain"])
    assert r.exit_code == 0, r.output
    assert "1_forward_not_simulated" in r.output and "[ ]" in r.output
    r = run.invoke(app, ["resume", "--note", "n/a", "-c", str(cfg_path)])
    assert r.exit_code == 0 and "Not halted" in r.output

    r = run.invoke(app, ["history", "inspect", str(csv)])
    assert r.exit_code == 0 and "proxyWallet" in r.output
