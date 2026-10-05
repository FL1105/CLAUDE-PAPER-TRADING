from __future__ import annotations

import pytest

from polybot.backtest import _buckets, placebo_sets, run_backtest
from polybot.config import load_config
from polybot.data.store import Store
from polybot.data.synthetic import DAY, T0, generate_world
from polybot.execution.paper import PaperExecutor, Signal
from polybot.execution.replay import drift_price_fn
from polybot.selection.walkforward import Epoch, leakage_selfcheck


def test_placebo_matches_buckets_and_excludes_selected() -> None:
    elig = {f"w{i}": (10 + i, 0.2 + 0.01 * i) for i in range(50)}
    b = _buckets(elig)
    epochs = [{"start_ts": 0, "end_ts": 10, "selected": ["w10", "w40"], "eligible": elig}]
    draws = placebo_sets(epochs, 100, seed=1)
    assert len(draws) == 100
    for sched in draws:
        ((_, _, chosen),) = sched
        assert len(chosen) == 2 and "w10" not in chosen and "w40" not in chosen
        assert {b[c] for c in chosen} == {b["w10"], b["w40"]}  # same activity/price bucket
    assert placebo_sets(epochs, 3, 1) == placebo_sets(epochs, 3, 1)  # seeded
    assert placebo_sets([{"start_ts": 0, "end_ts": 1, "selected": [], "eligible": {}}], 2, 0) == [
        [(0, 1, ())],
        [(0, 1, ())],
    ]


@pytest.fixture(scope="module")
def strong_run():  # type: ignore[no-untyped-def]
    c = load_config()
    sc = c.synthetic.model_copy(update={"skilled_scan_multiplier": 8.0, "sigma_noise": 0.15})
    ex = c.execution.model_copy(update={"sim_half_spread": 0.005})
    c = c.model_copy(
        update={
            "synthetic": sc,
            "execution": ex,
            "placebo": c.placebo.model_copy(update={"n_draws": 40}),
        }
    )
    w = generate_world(sc, 7, "skilled")
    r = run_backtest(
        w.trades,
        w.wf_markets,
        c,
        start_ts=T0 + 60 * DAY,
        end_ts=T0 + 180 * DAY,
        price_at=drift_price_fn(w.markets, 30.0),
        mode="synthetic",
        seed=7,
        config_hash="h",
        admissibility="simulation",
    )
    return c, w, r


def test_strong_world_evidence_and_gates(strong_run) -> None:  # type: ignore[no-untyped-def]
    _c, _w, r = strong_run
    assert r.placebo_computed and len(r.evidence.placebo_rois) == 40
    g = {x.name: x for x in r.gates}
    assert g["2_sample_size"].passed and g["3_net_edge_credible"].passed
    assert g["4_selection_adds_value"].passed  # real skill beats random matched wallets
    assert g["7_latency_robustness"].passed
    assert r.evidence.trades["epoch"].between(0, 7).all()


def test_strict_mode_never_promotes_synthetic(strong_run) -> None:  # type: ignore[no-untyped-def]
    from polybot.gates.gates import evaluate

    c, _, r = strong_run
    strict = {x.name: x.passed for x in evaluate(r.evidence, c, "strict")}
    assert not strict["1_forward_not_simulated"]


def test_leakage_selfcheck_on_clean_pipeline(strong_run) -> None:  # type: ignore[no-untyped-def]
    c, w, _ = strong_run
    ep = Epoch(0, T0 + 100 * DAY, T0 + 114 * DAY)
    assert leakage_selfcheck(w.trades, w.wf_markets, c, ep, 1) == 0


def test_halt_blocks_new_entries_but_not_exits() -> None:
    c = load_config()
    st = Store(":memory:")
    ex = PaperExecutor(st, c, evidence_mode="synthetic", config_hash="h")
    st.set_halt("drawdown")
    s = Signal(
        strategy="s",
        condition_id="c",
        token_id="t",
        ts=1,
        wallet="w",
        wallet_price=0.5,
        wallet_trade_key="k",
    )
    assert ex.handle(s, None).reason == "halted:drawdown"
    e = Signal(
        strategy="s",
        condition_id="c",
        token_id="t",
        ts=1,
        wallet="w",
        wallet_price=0.5,
        wallet_trade_key="k2",
        kind="info_exit",
    )
    assert ex.handle(e, None).status == "info"
    st.clear_halt("reviewed")
    assert st.get_halt() is None
    assert st.anomaly_counts() == {"kill_switch": 1, "kill_switch_cleared": 1}
