from datetime import date

import numpy as np
import pandas as pd

from bot.data import cache, quality, synthetic
from bot.data.calendar import nyse_holidays, nyse_sessions
from bot.data.providers import CsvProvider, SyntheticProvider, build_panel


def test_calendar_known_holidays():
    h = nyse_holidays(2024)
    assert date(2024, 3, 29) in h  # Good Friday
    assert date(2024, 11, 28) in h  # Thanksgiving
    assert date(2024, 7, 4) in h and date(2024, 12, 25) in h
    assert date(2022, 12, 26) in nyse_holidays(2022)  # Christmas observed on Monday
    assert date(2021, 12, 31) not in nyse_holidays(2021)  # Sat Jan 1 2022 not observed on Friday
    s = nyse_sessions("2024-07-01", "2024-07-08")
    assert pd.Timestamp("2024-07-04") not in s and len(s) == 5


def test_synthetic_deterministic_and_shaped():
    a = synthetic.make_prices(end="2008-12-31", seed=3)
    b = synthetic.make_prices(end="2008-12-31", seed=3)
    assert a["close"].equals(b["close"])
    c = synthetic.make_prices(end="2008-12-31", seed=4)
    assert not a["close"].equals(c["close"])
    r = a["close"].pct_change().dropna()
    assert (a["close"] > 0).all().all()
    vol = r.std() * np.sqrt(252)
    assert 0.05 < vol["TLT"] < 0.14 and 0.10 < vol["SPY"] < 0.30 and vol["BTC/USD"] > 0.4


def test_planted_edge_has_trend_autocorrelation():
    def trendiness(world):
        r = world["close"].pct_change().iloc[1:]
        m = r.rolling(60).mean().shift(1).dropna()
        return np.corrcoef(m.to_numpy().ravel(), r.loc[m.index].to_numpy().ravel())[0, 1]

    e = np.mean([trendiness(synthetic.make_prices(seed=s, planted_edge=True)) for s in range(4)])
    n = np.mean([trendiness(synthetic.make_prices(seed=s, planted_edge=False)) for s in range(4)])
    assert e > n + 0.003


def test_cache_atomic_incremental(tmp_path):
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
    cache.write_bars("BTC/USD", df, tmp_path)
    new = pd.DataFrame({"close": [2.5, 3.0]}, index=pd.to_datetime(["2020-01-03", "2020-01-06"]))
    out = cache.update_bars("BTC/USD", new, tmp_path)
    assert list(out["close"]) == [1.0, 2.5, 3.0]
    assert not list(tmp_path.glob("*.tmp"))


def test_csv_provider_adjusts(tmp_path):
    pd.DataFrame({"Date": ["2020-01-02", "2020-01-03"], "Close": [100, 50], "Adj Close": [50, 50],
                  "Open": [100, 50]}).to_csv(tmp_path / "SPY.csv", index=False)
    df = CsvProvider(tmp_path).bars("SPY", "2020-01-01", "2020-12-31")
    assert list(df["close"]) == [50, 50] and list(df["open"]) == [50, 50]


def test_quality_flags_problems():
    idx = nyse_sessions("2023-01-03", "2023-03-31").delete(5)  # one missing session
    c = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    c.iloc[20] = c.iloc[19] * 0.4  # split-like
    df = pd.DataFrame({"close": c, "volume": 1e6})
    kinds = {f.kind for f in quality.check_symbol("X", df, "equity")}
    assert {"missing_sessions", "split_like_jump"} <= kinds
    cidx = nyse_sessions("2023-01-03", "2023-03-28")
    wave = np.linspace(100, 101, len(cidx)) + np.sin(np.arange(len(cidx)))
    clean = pd.DataFrame({"close": wave}, index=cidx)
    assert quality.check_symbol("Y", clean, "equity") == []


def test_build_panel_from_synthetic():
    p = SyntheticProvider(seed=1, end="2007-12-31")
    panel = build_panel(p, ["SPY", "TLT"], "2005-01-01", "2007-12-31")
    assert set(panel) == {"close", "open", "volume", "cash"}
    assert panel["close"].shape[1] == 2 and "BIL" in panel["cash"]
