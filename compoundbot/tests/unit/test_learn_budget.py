import json

import pytest

from bot.config import load_budget
from bot.learn.budget import Budget, BudgetExceeded, combo_key, quarter_of

CFG = load_budget()


def _b(tmp_path, **kw):
    return Budget(CFG.model_copy(update=kw), tmp_path / "exp.jsonl")


def _combos(n, start=0):
    return [{"sma": 100 + i} for i in range(start, start + n)]


def test_quarter_labels():
    assert quarter_of("2025-01-02") == "2025Q1" and quarter_of("2025-12-31") == "2025Q4"


def test_founding_grid_is_free_then_new_variants_are_charged(tmp_path):
    b = _b(tmp_path, new_combos_per_quarter=5)
    r = b.reserve("trend", _combos(12), "d1", "2025-02-01")
    assert r == {"founding": 12, "new": 0, "known": 0} and b.used("2025-02-01") == 0
    r = b.reserve("trend", _combos(14), "d2", "2025-02-02")  # 2 genuinely new + 12 known
    assert r == {"founding": 0, "new": 2, "known": 12} and b.used("2025-02-02") == 2
    assert b.remaining("2025-02-02") == 3
    r = b.reserve("trend", _combos(14), "d3", "2025-02-03")  # re-evaluating known variants is free
    assert r["new"] == 0 and b.used("2025-02-03") == 2


def test_over_budget_is_refused_without_logging_anything(tmp_path):
    b = _b(tmp_path, new_combos_per_quarter=3)
    b.reserve("trend", _combos(4), "d1", "2025-02-01")
    before = (tmp_path / "exp.jsonl").read_text()
    with pytest.raises(BudgetExceeded) as e:
        b.reserve("trend", _combos(9), "d2", "2025-02-02")  # 5 new > 3
    assert "next quarter" in str(e.value)
    assert (tmp_path / "exp.jsonl").read_text() == before
    # exactly fitting is allowed
    b.reserve("trend", _combos(7), "d2", "2025-02-02")
    assert b.remaining("2025-02-02") == 0


def test_budget_resets_each_quarter_and_known_combos_persist(tmp_path):
    b = _b(tmp_path, new_combos_per_quarter=2)
    b.reserve("trend", _combos(3), "d1", "2025-02-01")
    b.reserve("trend", _combos(5), "d2", "2025-02-02")
    assert b.remaining("2025-02-02") == 0
    assert b.remaining("2025-04-02") == 2  # new quarter
    r = b.reserve("trend", _combos(5), "d3", "2025-04-02")
    assert r["new"] == 0  # nothing new: the earlier ones are remembered


def test_dsr_bar_rises_with_use_and_is_capped(tmp_path):
    b = _b(tmp_path, new_combos_per_quarter=100, dsr_step=0.02, dsr_step_every=4, dsr_min_cap=0.95)
    b.reserve("trend", _combos(1), "d", "2025-02-01")
    assert b.dsr_min("2025-02-01") == pytest.approx(CFG.dsr_min_base)
    b.reserve("trend", _combos(1 + 9), "d", "2025-02-02")  # 9 new -> 2 steps
    assert b.dsr_min("2025-02-02") == pytest.approx(CFG.dsr_min_base + 0.04)
    b.reserve("trend", _combos(1 + 60), "d", "2025-02-03")
    assert b.dsr_min("2025-02-03") == 0.95  # cap
    assert b.dsr_min("2025-05-01") == pytest.approx(CFG.dsr_min_base)  # fresh quarter, fresh bar


def test_strategies_are_independent_for_founding(tmp_path):
    b = _b(tmp_path, new_combos_per_quarter=1)
    b.reserve("trend", _combos(5), "d", "2025-02-01")
    r = b.reserve("momentum", [{"top_n": 3}, {"top_n": 4}], "d", "2025-02-01")
    assert r["founding"] == 2 and b.used("2025-02-01") == 0


def test_status_and_log_format(tmp_path):
    b = _b(tmp_path, new_combos_per_quarter=4)
    b.reserve("trend", _combos(2), "d1", "2025-02-01")
    b.reserve("trend", _combos(4), "d2", "2025-02-01")
    st = b.status("2025-02-01")
    assert st["used"] == 2 and st["remaining"] == 2 and st["combos_ever"] == 4 and st["quarter"] == "2025Q1"
    recs = [json.loads(x) for x in (tmp_path / "exp.jsonl").read_text().splitlines()]
    assert {r["stage"] for r in recs} == {"founding", "charge"}
    assert combo_key("trend", {"sma": 100}) in b.known()
