import pytest

from bot.backtest.walkforward import research, split_holdout, trial_counts
from bot.config import load_costs, load_risk, load_strategy, load_universe
from bot.data.synthetic import make_prices
from bot.strategies import make_registry


def _run(edge, seed, tmp_path, only):
    p = make_prices(seed=seed, planted_edge=edge)
    dev, hold, cut = split_holdout(p, 2)
    assert dev["close"].index.max() < cut <= hold["close"].index.min()  # holdout untouched
    dev = {k: dev[k] for k in ("close", "open", "cash", "volume")}
    cfg = load_strategy()
    return research(dev, load_universe(), load_risk(), load_costs(), make_registry(cfg), cfg.walkforward,
                    tmp_path / "exp.jsonl", "SYNTHETIC", n_placebo=20, n_boot=150, only=only)


def test_holdout_split_is_disjoint_and_two_years():
    p = make_prices(end="2012-12-31", seed=1)
    dev, hold, cut = split_holdout(p, 2)
    assert len(set(dev["close"].index) & set(hold["close"].index)) == 0
    assert 1.9 < (hold["close"].index[-1] - cut).days / 365.25 < 2.1


@pytest.mark.slow
def test_harness_keeps_planted_edge(tmp_path):
    res = _run(True, 1, tmp_path, ["trend"])
    assert res["outcomes"]["trend"].verdict == "KEEP"
    n, var = trial_counts(tmp_path / "exp.jsonl")
    assert n == 12 and var > 0  # every grid combination was logged


@pytest.mark.slow
def test_harness_rejects_null_world(tmp_path):
    res = _run(False, 1, tmp_path, ["trend"])
    assert res["outcomes"]["trend"].verdict == "REJECT"
