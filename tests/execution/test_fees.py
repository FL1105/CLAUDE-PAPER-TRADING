from __future__ import annotations

import pytest

from polybot.config import Config
from polybot.execution.fees import fee_coeff, fill_fee, level_fee


def test_parabolic_shape() -> None:
    assert level_fee(100, 0.5, 0.05) == pytest.approx(1.25)
    assert level_fee(100, 0.1, 0.05) == pytest.approx(0.45)
    assert level_fee(100, 0.0, 0.05) == 0.0 and level_fee(100, 1.0, 0.05) == 0.0
    assert level_fee(100, 0.3, 0.05) == pytest.approx(level_fee(100, 0.7, 0.05))
    with pytest.raises(ValueError):
        level_fee(-1, 0.5, 0.05)
    with pytest.raises(ValueError):
        level_fee(1, 1.5, 0.05)


def test_fill_fee_sums_levels() -> None:
    assert fill_fee([(0.5, 100), (0.6, 50)], 0.05) == pytest.approx(1.25 + 0.6)
    assert fill_fee([], 0.05) == 0.0


def test_coeff_by_category_zero_and_stress(cfg: Config) -> None:
    fc = cfg.fees.model_copy(
        update={"per_category": {"crypto": 0.07}, "zero_fee_categories": ("sports",)}
    )
    assert fee_coeff(fc, None) == 0.05
    assert fee_coeff(fc, "politics") == 0.05
    assert fee_coeff(fc, "crypto") == 0.07
    assert fee_coeff(fc, "crypto", 2.0) == pytest.approx(0.14)
    assert fee_coeff(fc, "sports", 2.0) == 0.0
    assert fee_coeff(fc.model_copy(update={"formula": "zero"}), "crypto") == 0.0
    with pytest.raises(ValueError):
        fee_coeff(fc, None, -1)
