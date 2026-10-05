"""Phase 2 acceptance checks at reduced seed counts (full 300-seed run: -m slow)."""

from __future__ import annotations

import json

import pytest

from polybot.config import Config
from polybot.experiments import null_calibration, selection_bias_demo, selection_bias_over_seeds


@pytest.fixture
def small_cfg(cfg: Config) -> Config:
    sc = cfg.synthetic.model_copy(update={"calibration_mc_sims": 500})
    return cfg.model_copy(update={"synthetic": sc})


def test_null_world_pvalues_uniform_and_bh_quiet(small_cfg: Config) -> None:
    r = null_calibration(small_cfg, list(range(100, 120)))
    assert r["per_seed_ks"]["fail_rate_at_0.01"] <= 0.10
    assert r["pooled_ks"]["p"] > 0.001
    assert 0.035 < r["rejection_rate"]["at_0.05"] < 0.065
    assert r["bh"]["rate"] <= 0.3  # bound is q = 0.10; generous for 20 seeds
    assert r["small_n"]["exact_mc_reject_rate_at_alpha"] <= 0.06


def test_naive_selection_is_selection_bias_in_null_world(small_cfg: Config) -> None:
    s = selection_bias_over_seeds(small_cfg, list(range(200, 206)), "null")
    naive = s["naive_top_pnl"]
    assert naive["in_sample_roi"]["mean"] > 0.3  # looks great in-sample...
    assert abs(naive["oos_roi"]["mean"]) < 0.1  # ...and is nothing out of sample
    assert s["bh"]["n_selected"]["mean"] <= 1.0  # BH (almost) never selects anyone


def test_bh_finds_skill_in_strong_skilled_world(small_cfg: Config) -> None:
    # Default skill is too weak for per-wallet detection at these sample sizes (a
    # documented finding); the detection path is exercised with more active skilled wallets.
    sc = small_cfg.synthetic.model_copy(update={"skilled_scan_multiplier": 8.0})
    cfg = small_cfg.model_copy(update={"synthetic": sc})
    r = selection_bias_demo(cfg, 300, "skilled")
    assert r["bh"]["oos"]["n_wallets"] >= 2
    assert r["bh"]["false_discovery_proportion"] <= 0.5
    assert r["bh"]["oos"]["roi"] > 0 and r["bh"]["oos"]["pooled_z"] > 1.64


def test_byte_identical_reports(small_cfg: Config) -> None:
    a = json.dumps(null_calibration(small_cfg, [1, 2]), sort_keys=True)
    b = json.dumps(null_calibration(small_cfg, [1, 2]), sort_keys=True)
    assert a == b


@pytest.mark.slow
def test_full_300_seed_null_calibration(cfg: Config) -> None:
    r = null_calibration(cfg, [cfg.seed + i for i in range(cfg.synthetic.acceptance_null_seeds)])
    assert r["per_seed_ks"]["fail_rate_wilson95"][0] <= 0.01
    assert r["bh"]["rate_wilson95"][0] <= cfg.stats.fdr_q
