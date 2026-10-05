"""Walk-forward + LEAKAGE tests. If any leakage test fails, the build must fail."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from polybot.config import Config, load_config
from polybot.data.synthetic import DAY, T0, SyntheticWorld, generate_world
from polybot.selection.walkforward import (
    DataView,
    Epoch,
    Selection,
    make_epochs,
    make_leaky_selector,
    select_wallets,
    walk_forward,
)

START, END = T0 + 60 * DAY, T0 + 180 * DAY


@pytest.fixture(scope="module")
def cfgm() -> Config:
    c = load_config()
    # strong, clearly detectable skill: these tests check mechanism, not real-world power
    sc = c.synthetic.model_copy(
        update={"calibration_mc_sims": 300, "skilled_scan_multiplier": 8.0, "sigma_noise": 0.15}
    )
    return c.model_copy(update={"synthetic": sc})


@pytest.fixture(scope="module")
def null_world(cfgm: Config) -> SyntheticWorld:
    return generate_world(cfgm.synthetic, 11, "null")


@pytest.fixture(scope="module")
def skilled_world(cfgm: Config) -> SyntheticWorld:
    return generate_world(cfgm.synthetic, 12, "skilled")


def test_make_epochs() -> None:
    eps = make_epochs(0, 30 * DAY, 14)
    assert [(e.start_ts, e.end_ts) for e in eps] == [(0, 14 * DAY), (14 * DAY, 28 * DAY)]
    with pytest.raises(ValueError):
        make_epochs(5, 5, 14)


def test_dataview_contains_nothing_from_the_future(null_world: SyntheticWorld) -> None:
    cut = T0 + 90 * DAY
    v = DataView(null_world.trades, null_world.wf_markets, cut)
    t, m = v.trades(), v.markets()
    assert t["ts"].max() < cut
    late_ids = null_world.markets.loc[null_world.markets.end_ts >= cut, "market"]
    late = m["market"].isin(late_ids)
    assert late.any()
    assert m.loc[late, "winning_outcome"].isna().all()
    assert m.loc[late, "resolved_ts"].isna().all()
    assert (m.loc[late, "status"] == "open").all()
    assert not hasattr(v, "__dict__")  # __slots__: nothing else can be stashed on it
    t.loc[:, "ts"] = 0  # mutating a returned copy cannot alter the view
    assert v.trades()["ts"].max() > 0


def test_selector_requires_view_cut_at_epoch_start(
    cfgm: Config, null_world: SyntheticWorld
) -> None:
    ep = Epoch(0, START, START + 14 * DAY)
    with pytest.raises(ValueError):
        select_wallets(DataView(null_world.trades, null_world.wf_markets, START + 1), ep, cfgm, 0)


def test_LEAKAGE_selection_invariant_to_any_change_in_the_future(  # noqa: N802
    cfgm: Config, skilled_world: SyntheticWorld
) -> None:
    """Rewrite everything at/after the cutoff: outcomes, resolution times, trades.
    The selection must be identical. If this fails, the pipeline leaks."""
    w = skilled_world
    ep = Epoch(0, T0 + 100 * DAY, T0 + 114 * DAY)
    base = select_wallets(DataView(w.trades, w.wf_markets, ep.start_ts), ep, cfgm, 1)
    assert base.wallets, "need a non-empty selection for a meaningful leakage test"

    m2 = w.wf_markets.copy()
    fut = m2["resolved_ts"] >= ep.start_ts
    m2.loc[fut, "winning_outcome"] = 1 - m2.loc[fut, "winning_outcome"]  # flip every future result
    m2.loc[fut, "resolved_ts"] = m2.loc[fut, "resolved_ts"] + 7 * DAY
    t2 = w.trades.copy()
    after = t2["ts"] >= ep.start_ts
    t2.loc[after, "price"] = 0.5
    rng = np.random.default_rng(0)
    extra = t2.loc[after].sample(500, random_state=1)
    extra = extra.assign(
        wallet=rng.choice(np.asarray(base.wallets), len(extra)), ts=ep.start_ts + 5
    )
    t2 = pd.concat([t2, extra], ignore_index=True)

    again = select_wallets(DataView(t2, m2, ep.start_ts), ep, cfgm, 1)
    assert again.wallets == base.wallets
    assert again.naive == base.naive
    assert again.cap == base.cap
    assert again.diagnostics["units"] == base.diagnostics["units"]


def test_LEAKAGE_markets_resolving_after_cutoff_are_purged(  # noqa: N802
    cfgm: Config, null_world: SyntheticWorld
) -> None:
    ep = Epoch(0, T0 + 100 * DAY, T0 + 114 * DAY)
    v = DataView(null_world.trades, null_world.wf_markets, ep.start_ts)
    sel = select_wallets(v, ep, cfgm, 0)
    assert sel.diagnostics["purged_trades_unresolved_markets"] > 0
    mk = null_world.markets
    lo = ep.start_ts - cfgm.walkforward.trailing_window_days * DAY
    window = mk.loc[(mk.end_ts < ep.start_ts) & (mk.end_ts >= lo)]
    assert sel.diagnostics["markets_in_window"] == len(window)


def test_LEAKAGE_canary_inflates_and_real_selector_cannot_reproduce(  # noqa: N802
    cfgm: Config, null_world: SyntheticWorld
) -> None:
    w = null_world
    leaky = walk_forward(
        w.trades,
        w.wf_markets,
        cfgm,
        START,
        END,
        seed=0,
        selector=make_leaky_selector(w.trades, w.wf_markets, cfgm),
    )
    real = walk_forward(w.trades, w.wf_markets, cfgm, START, END, seed=0)
    lk: dict[str, Any] = leaky.aggregate["selected"]
    assert lk["roi"] > 0.5 and lk["frac_wallets_positive_oos"] > 0.9  # obviously inflated
    rs = real.aggregate["selected"]
    assert rs["roi"] is None or abs(rs["roi"]) < 0.15
    nv = real.aggregate["naive"]  # honest naive selection: no OOS edge in the null world
    assert abs(nv["roi"]) < 0.2 and nv["roi"] < lk["roi"] / 3


def test_skilled_world_walk_forward_finds_persistent_edge(
    cfgm: Config, skilled_world: SyntheticWorld
) -> None:
    r = walk_forward(skilled_world.trades, skilled_world.wf_markets, cfgm, START, END, seed=0)
    s = r.aggregate["selected"]
    assert r.aggregate["n_epochs"] == 8
    assert s["epochs_with_signals"] >= 5
    assert s["roi"] > 0.1 and s["mean_wallet_oos_z"] > 0.5
    assert s["frac_wallets_positive_oos"] > 0.6
    assert set(r.epochs[0]) >= {
        "selected",
        "oos_roi",
        "naive_oos_roi",
        "diagnostics",
        "pending_unresolved_trades",
    }


def test_wrong_epoch_from_selector_rejected(cfgm: Config, null_world: SyntheticWorld) -> None:
    def bad(view: DataView, ep: Epoch) -> Selection:
        return Selection(Epoch(99, 0, 1), (), (), 1.0)

    with pytest.raises(ValueError):
        walk_forward(
            null_world.trades, null_world.wf_markets, cfgm, START, END, seed=0, selector=bad
        )
