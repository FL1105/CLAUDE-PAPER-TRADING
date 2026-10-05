from __future__ import annotations

import pandas as pd

from polybot.config import Config
from polybot.selection.filters import apply_filters, wash_flags
from polybot.stats.edge import build_units

DAY = 86_400


def wallet_trades(
    wallet: str,
    n: int,
    *,
    price: float = 0.5,
    span_days: int = 40,
    start_market: int = 0,
    per_trade: int = 1,
) -> list[tuple]:
    rows = []
    for i in range(n):
        for k in range(per_trade):
            rows.append(
                (wallet, start_market + i, 0, "BUY", price, 10.0, i * span_days * DAY // n + k)
            )
    return rows


def run(cfg: Config, rows: list[tuple], winners: dict[int, int] | None = None):
    t = pd.DataFrame(rows, columns=["wallet", "market", "outcome", "side", "price", "shares", "ts"])
    mk = sorted(t.market.unique())
    w = pd.DataFrame({"market": mk, "winning_outcome": [(winners or {}).get(m, m % 2) for m in mk]})
    res_ts = pd.Series([10**9] * len(mk), index=mk)
    units = build_units(t, w)
    return apply_filters(
        t,
        units,
        res_ts,
        cfg.filters,
        cap_quantile=0.95,
        small_n_exact_threshold=30,
        seed=0,
        n_mc=200,
    )


def steps(r) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return {s.name: s.removed for s in r.steps}


def test_good_wallet_survives(cfg: Config) -> None:
    r = run(cfg, wallet_trades("good", 40))
    assert r.eligible == ["good"]


def test_each_filter_removes_its_target(cfg: Config) -> None:
    rows = wallet_trades("good", 40)
    rows += wallet_trades("few", 10, start_market=100)
    rows += wallet_trades("short", 40, span_days=5, start_market=200)
    rows += wallet_trades("penny", 40, price=0.97, start_market=300)
    rows += wallet_trades("busy", 40, start_market=400, per_trade=300)
    mm = wallet_trades("mm", 40, start_market=500)
    rows += mm + [(w, m, 1, s, p, q, t + 1) for (w, m, _, s, p, q, t) in mm]
    rows += wallet_trades("flipper", 40, start_market=600)
    rows += [("flipper", 600 + i, 0, "SELL", 0.5, 10.0, i * DAY + 5) for i in range(40)]
    r = run(cfg, rows)
    s = steps(r)
    assert r.eligible == ["good"]
    assert s["market_maker_two_sided"] == 1
    assert s["no_entries_left_after_price_band"] == 1  # penny picker
    assert s["entry_price_band"] == 40
    assert s["trades_per_day"] == 1
    assert s["closed_before_resolution"] == 1
    assert s["min_resolved_markets"] == 1
    assert s["min_active_span_days"] == 1


def test_one_hit_wonder_removed(cfg: Config) -> None:
    rows = wallet_trades("lucky", 40, price=0.1)
    winners = {i: 1 for i in range(40)}
    winners[0] = 0  # one 10x win, everything else loses
    r = run(cfg, rows, winners)
    assert steps(r)["single_market_pnl_share"] == 1 and r.eligible == []


def test_wash_trading_flagged_not_dropped(cfg: Config) -> None:
    rows = wallet_trades("washy", 40)
    rows += [("washy", 0, 0, "SELL", 0.5, 10.0, k) for k in range(1, 4)]
    rows += [("washy", 0, 0, "BUY", 0.5, 10.0, k + 1) for k in range(1, 4)]
    t = pd.DataFrame(rows, columns=["wallet", "market", "outcome", "side", "price", "shares", "ts"])
    assert list(wash_flags(t).wallet) == ["washy"]
    cfg2 = cfg.model_copy(
        update={
            "filters": cfg.filters.model_copy(update={"max_closed_before_resolution_ratio": 1.0})
        }
    )
    r = run(cfg2, rows)
    assert "washy" in r.eligible and list(r.flags.wallet) == ["washy"]
    assert wash_flags(t.loc[t.side == "BUY"]).empty
