"""The placebo uses fastsim; this asserts it is the SAME engine as replay."""

from __future__ import annotations

import pytest

from polybot.config import load_config
from polybot.data.synthetic import DAY, T0, generate_world
from polybot.execution.fastsim import fast_copy, prepare_signals
from polybot.execution.replay import drift_price_fn, replay
from polybot.selection.walkforward import walk_forward


@pytest.fixture(scope="module")
def setup():  # type: ignore[no-untyped-def]
    c = load_config()
    sc = c.synthetic.model_copy(update={"skilled_scan_multiplier": 8.0, "sigma_noise": 0.15})
    c = c.model_copy(update={"synthetic": sc})
    w = generate_world(sc, 21, "skilled")
    wf = walk_forward(w.trades, w.wf_markets, c, T0 + 60 * DAY, T0 + 180 * DAY, seed=21)
    eps = [(e["start_ts"], e["end_ts"], tuple(e["selected"])) for e in wf.epochs]
    return c, w, eps


@pytest.mark.parametrize(
    "h,kw",
    [
        (30.0, {}),
        (2.0, {}),
        (30.0, {"fee_multiplier": 2.0, "spread_multiplier": 2.0, "latency_multiplier": 2.0}),
    ],
)
def test_fast_copy_equals_replay(setup, h: float, kw: dict) -> None:  # type: ignore[no-untyped-def]
    c, w, eps = setup
    pf = drift_price_fn(w.markets, h)
    r = replay(w.trades, w.markets, eps, c, pf, evidence_mode="synthetic", config_hash="x", **kw)
    f = fast_copy(prepare_signals(w.trades, eps, c), w.markets, c, pf, **kw)
    s = r.summary
    assert (f.signals, f.filled, f.capacity_skipped, f.settled) == (
        s["signals"],
        s["filled"],
        s["capacity_skipped"],
        s["settled"],
    )
    assert f.roi == pytest.approx(s["roi_net"], rel=1e-9, abs=1e-12)
    assert f.fill_rate == pytest.approx(s["fill_rate"])
