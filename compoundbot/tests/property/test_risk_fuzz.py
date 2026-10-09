import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bot.config import load_risk, load_universe
from bot.risk.drawdown import CASH, FULL, HALF, HALT, Ladder
from bot.risk.killswitch import KillSwitch, KillSwitchEngaged
from bot.risk.limits import check_order, clip_weights, daily_loss_exceeded, data_healthy

RISK = load_risk()
U = load_universe()
CLASSES = {a.symbol: a.cls for a in U.assets}

weird = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True, width=64),
    st.floats(min_value=-5, max_value=5),
    st.just(0.0),
)


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(st.sampled_from(sorted(CLASSES)), weird, min_size=1))
def test_clip_weights_never_breaks_limits(raw):
    w, _ = clip_weights(raw, CLASSES, RISK)
    assert all(math.isfinite(x) and x >= 0 for x in w.values())
    assert all(x <= RISK.max_weight_per_asset + 1e-12 for x in w.values())
    assert sum(w.values()) <= RISK.gross_exposure_max + 1e-9
    crypto = sum(x for s, x in w.items() if CLASSES[s] == "crypto")
    assert crypto <= RISK.crypto_sleeve_max + 1e-9


@settings(max_examples=400, deadline=None)
@given(
    qty=weird, price=weird, equity=weird,
    adv=st.one_of(st.none(), weird), limit=st.one_of(st.none(), weird),
    side=st.sampled_from(["buy", "sell"]), cur=st.floats(min_value=0, max_value=1e7),
)
def test_check_order_accepts_only_safe_orders(qty, price, equity, adv, limit, side, cur):
    chk = check_order("SPY", side, qty, price, limit, equity, adv, RISK, current_value=cur)
    if chk.ok:
        assert all(math.isfinite(x) and x > 0 for x in (qty, price, equity))
        notional = qty * price
        if side == "buy":  # reducing sells are exempt from size/ADV caps by design
            assert notional <= (RISK.max_weight_per_asset + RISK.order_cap_over_max_weight) * equity + 1e-6
            if adv is not None and math.isfinite(adv) and adv > 0:
                assert notional <= RISK.order_cap_adv_frac * adv + 1e-6
        if limit is not None:
            assert abs(limit / price - 1) <= RISK.price_collar + 1e-9
        if side == "sell":
            assert notional <= cur * 1.0001 + 1e-6


def test_daily_loss_halt_and_failsafe():
    assert daily_loss_exceeded(100.0, 96.9, RISK)
    assert not daily_loss_exceeded(100.0, 97.5, RISK)
    assert daily_loss_exceeded(float("nan"), 100.0, RISK)
    assert daily_loss_exceeded(0.0, 0.0, RISK)


def test_data_health_blocks_stale():
    lag = lambda d, today: today - d  # noqa: E731
    ok, bad = data_healthy({"SPY": 10, "TLT": 7}, ["SPY", "TLT", "GLD"], 10, 1, lag)
    assert not ok and set(bad) == {"TLT", "GLD"}
    assert data_healthy({"SPY": 10}, ["SPY"], 10, 1, lag)[0]


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(min_value=-0.2, max_value=0.2), min_size=5, max_size=300),
       st.lists(st.booleans(), min_size=300, max_size=300))
def test_ladder_invariants(rets, majority):
    lad = Ladder(RISK, auto_rearm=True)
    eq = 1.0
    for i, r in enumerate(rets):
        eq *= 1 + r
        m = lad.update(eq, majority[i])
        assert m in (0.0, 0.5, 1.0)
        dd = 1 - eq / lad.peak
        if dd >= RISK.drawdown_halt_at:
            assert lad.level == HALT and m == 0.0
        elif dd >= RISK.drawdown_cash_at:
            assert lad.level in (CASH, HALT)
        elif dd >= RISK.drawdown_halve_at:
            assert lad.level in (HALF, CASH, HALT)


def test_ladder_sequence_and_manual_rearm():
    lad = Ladder(RISK, auto_rearm=False)
    assert lad.update(0.95, True) == 1.0
    assert lad.update(0.91, True) == 0.5  # -9%
    assert lad.update(0.87, True) == 0.0  # -13% -> CASH
    assert lad.level == CASH
    assert lad.update(0.84, True) == 0.0  # -16% -> HALT
    assert lad.level == HALT
    for _ in range(500):  # no auto re-arm in live mode
        assert lad.update(0.99, True) == 0.0
    lad.rearm(0.99)
    assert lad.level == HALF and lad.peak == 0.99


def test_ladder_rerisk_rules():
    lad = Ladder(RISK)
    lad.update(0.91, False)
    assert lad.level == HALF
    assert lad.update(0.95, False) == 0.5  # -5%: not within 4% and trend not majority
    assert lad.update(0.97, False) == 1.0  # within 4% of peak: back to FULL
    lad2 = Ladder(RISK)
    lad2.update(0.91, False)
    for _ in range(RISK.rerisk_min_days):
        lad2.update(0.915, False)
    assert lad2.level == HALF  # time alone is not enough without trend breadth
    lad2.update(0.915, True)
    assert lad2.level == FULL


def test_killswitch_persists_and_blocks(tmp_path):
    ks = KillSwitch(tmp_path / "KILL")
    ks.assert_clear()
    ks.engage("test drill", by="pytest")
    assert KillSwitch(tmp_path / "KILL").is_engaged()  # survives a "restart"
    with pytest.raises(KillSwitchEngaged):
        ks.assert_clear()
    with pytest.raises(ValueError):
        ks.release("")
    ks.release("operator")
    ks.assert_clear()


def test_forced_flatten_sell_is_not_blocked_by_size_cap():
    chk = check_order("TLT", "sell", 100, 100.0, 99.5, 10_000.0, 1e6, RISK, current_value=10_000.0)
    assert chk.ok  # 100% of equity, far above the 30% buy cap and 1% of ADV
    assert not check_order("TLT", "buy", 100, 100.0, 100.5, 10_000.0, None, RISK).ok
    assert not check_order("TLT", "sell", 101, 100.0, 99.5, 10_000.0, None, RISK, current_value=10_000.0).ok
