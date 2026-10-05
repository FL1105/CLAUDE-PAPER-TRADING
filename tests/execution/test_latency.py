from __future__ import annotations

import numpy as np
import pytest

from polybot.execution.latency import drift_price, synthetic_asks, tape_price


def test_drift_half_life() -> None:
    assert drift_price(0.5, 0.7, 0, 30) == 0.5
    assert drift_price(0.5, 0.7, 30, 30) == pytest.approx(0.6)
    assert drift_price(0.5, 0.7, 60, 30) == pytest.approx(0.65)
    assert drift_price(0.5, 0.7, 1e6, 30) == pytest.approx(0.7)
    assert drift_price(0.5, 0.3, 30, 30) == pytest.approx(0.4)
    with pytest.raises(ValueError):
        drift_price(0.5, 0.7, -1, 30)


def test_synthetic_asks() -> None:
    a = synthetic_asks(0.5, half_spread=0.01, levels=3, depth_usdc=100, tick=0.01)
    assert [lv.price for lv in a] == [0.51, 0.52, 0.53]
    assert a[0].price * a[0].size == pytest.approx(100)
    assert (
        len(synthetic_asks(0.97, half_spread=0.01, levels=5, depth_usdc=10, tick=0.01)) == 2
    )  # 0.98, 0.99
    with pytest.raises(ValueError):
        synthetic_asks(0.5, half_spread=0.01, levels=0, depth_usdc=100, tick=0.01)


def test_tape_price_never_uses_pre_signal_prices() -> None:
    ts = np.array([10, 20, 30, 40], dtype=np.int64)
    px = np.array([0.1, 0.2, 0.3, 0.4])
    assert tape_price(ts, px, at_ts=35, after_ts=20) == 0.3
    assert tape_price(ts, px, at_ts=25, after_ts=21) is None  # only the pre-signal 0.2 exists
    assert tape_price(ts, px, at_ts=40, after_ts=0) == 0.4
    with pytest.raises(ValueError):
        tape_price(ts, px[:2], at_ts=1, after_ts=0)
