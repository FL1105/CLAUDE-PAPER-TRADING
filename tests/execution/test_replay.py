from __future__ import annotations

import pandas as pd
import pytest

from polybot.config import Config, load_config
from polybot.data.synthetic import DAY, T0, generate_world
from polybot.execution.replay import drift_price_fn, replay, tape_price_fn
from polybot.selection.walkforward import walk_forward


def trades(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        rows, columns=["wallet", "market", "outcome", "side", "price", "shares", "ts"]
    )


MK = pd.DataFrame(
    {"market": [1, 2], "winning_outcome": [0.0, 1.0], "resolved_ts": [500, 900], "q": [0.6, 0.4]}
)


def test_chronological_settlement_frees_capacity(cfg: Config) -> None:
    pc = cfg.paper.model_copy(update={"max_concurrent_positions": 1})
    c = cfg.model_copy(update={"paper": pc})
    t = trades([("w", 1, 0, "BUY", 0.5, 10, 100), ("w", 2, 0, "BUY", 0.5, 10, 600)])
    r = replay(
        t, MK, [(0, 1000, ("w",))], c, lambda tr, d: 0.5, evidence_mode="backtest", config_hash="h"
    )
    assert r.summary["filled"] == 2 and r.summary["capacity_skipped"] == 0
    # market 1 (outcome 0 won) -> profit; market 2 (outcome 1 won) -> loss
    pnl = r.settlements.net_pnl.tolist()
    assert pnl[0] > 0 > pnl[1]
    assert r.settlements.resolved_ts.tolist() == [500, 900]


def test_capacity_skips_do_not_count_against_fill_rate(cfg: Config) -> None:
    pc = cfg.paper.model_copy(update={"max_concurrent_positions": 1})
    c = cfg.model_copy(update={"paper": pc})
    t = trades([("w", 1, 0, "BUY", 0.5, 10, 100), ("w", 2, 0, "BUY", 0.5, 10, 200)])
    r = replay(
        t, MK, [(0, 1000, ("w",))], c, lambda tr, d: 0.5, evidence_mode="backtest", config_hash="h"
    )
    assert r.summary["capacity_skipped"] == 1 and r.summary["fill_rate"] == 1.0


def test_filters_band_wallets_epochs_and_unresolved(cfg: Config) -> None:
    mk = pd.concat(
        [
            MK,
            pd.DataFrame(
                {
                    "market": [3],
                    "winning_outcome": [float("nan")],
                    "resolved_ts": [float("nan")],
                    "q": [0.5],
                }
            ),
        ]
    )
    t = trades(
        [
            ("w", 1, 0, "BUY", 0.97, 10, 100),  # outside entry band
            ("x", 1, 0, "BUY", 0.5, 10, 100),  # not frozen
            ("w", 1, 0, "SELL", 0.5, 10, 100),  # exits are not copied
            ("w", 2, 0, "BUY", 0.5, 10, 2000),  # outside epoch
            ("w", 3, 0, "BUY", 0.5, 10, 150),  # never resolves -> stays open
        ]
    )
    r = replay(
        t,
        mk,
        [(0, 1000, ("w",))],
        cfg,
        lambda tr, d: 0.5,
        evidence_mode="backtest",
        config_hash="h",
    )
    assert r.summary["signals"] == 1 and r.summary["settled"] == 0
    assert r.summary["roi_net"] is None


def test_no_reference_price_is_skipped_not_guessed(cfg: Config) -> None:
    t = trades([("w", 1, 0, "BUY", 0.5, 10, 100)])
    r = replay(
        t,
        MK,
        [(0, 1000, ("w",))],
        cfg,
        lambda tr, d: None,
        evidence_mode="backtest",
        config_hash="h",
    )
    assert r.summary["skip_reasons"] == {"empty_book": 1}


def test_tape_price_fn_only_uses_post_signal_tape() -> None:
    t = trades(
        [
            ("a", 1, 0, "BUY", 0.40, 10, 100),
            ("b", 1, 0, "BUY", 0.45, 10, 101),
            ("c", 1, 0, "BUY", 0.60, 10, 110),
            ("d", 1, 1, "BUY", 0.99, 10, 101),
        ]
    )
    f = tape_price_fn(t)
    row = {"market": 1, "outcome": 0, "ts": 101}
    assert f(row, 2) == 0.45  # the signal's own trade (same ts) is the latest at t+2
    assert f(row, 10) == 0.60
    assert f({"market": 1, "outcome": 0, "ts": 200}, 2) is None


def test_drift_fn_uses_token_side() -> None:
    f = drift_price_fn(MK, 30.0)
    assert f({"market": 1, "outcome": 0, "price": 0.5}, 1e9) == pytest.approx(0.6)
    assert f({"market": 1, "outcome": 1, "price": 0.5}, 1e9) == pytest.approx(0.4)


@pytest.fixture(scope="module")
def strong() -> tuple[Config, object, list]:
    c = load_config()
    sc = c.synthetic.model_copy(
        update={"skilled_scan_multiplier": 8.0, "sigma_noise": 0.15, "calibration_mc_sims": 300}
    )
    c = c.model_copy(update={"synthetic": sc})
    w = generate_world(sc, 7, "skilled")
    wf = walk_forward(w.trades, w.wf_markets, c, T0 + 60 * DAY, T0 + 180 * DAY, seed=7)
    eps = [(e["start_ts"], e["end_ts"], tuple(e["selected"])) for e in wf.epochs]
    return c, w, eps


def test_skilled_low_latency_edge_survives_costs(strong) -> None:  # type: ignore[no-untyped-def]
    c, w, eps = strong
    r = replay(
        w.trades,
        w.markets,
        eps,
        c,
        drift_price_fn(w.markets, 30.0),
        evidence_mode="synthetic",
        config_hash="h",
    )
    s = r.summary
    assert s["fill_rate"] > 0.85
    assert 0 < s["roi_net"] < s["roi_gross_at_wallet_price"]  # costs bite, edge survives
    lat = s["roi_by_latency"]
    assert lat["1xL"] > lat["5xL"]  # edge decays with latency


def test_skilled_high_latency_is_not_fillable(strong) -> None:  # type: ignore[no-untyped-def]
    c, w, eps = strong
    r = replay(
        w.trades,
        w.markets,
        eps,
        c,
        drift_price_fn(w.markets, 0.4),
        evidence_mode="synthetic",
        config_hash="h",
    )
    assert r.summary["fill_rate"] < 0.5  # chase rule: edge priced in before we can fill
