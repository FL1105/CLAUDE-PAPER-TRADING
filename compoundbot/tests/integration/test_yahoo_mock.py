import httpx
import pandas as pd
import pytest

from bot.data.providers import YahooProvider


def _client(payload, status=200):
    def handler(req: httpx.Request) -> httpx.Response:
        handler.last = req
        return httpx.Response(status, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handler)), handler


def _payload(adj=True):
    ts = [int(pd.Timestamp(d, tz="America/New_York").replace(hour=9, minute=30).timestamp())
          for d in ("2024-01-02", "2024-01-03", "2024-01-04")]
    res = {"timestamp": ts, "indicators": {"quote": [{"open": [100, 50, 51], "high": [101, 52, 53],
                                                      "low": [99, 49, 50], "close": [100, 50, 52],
                                                      "volume": [10, 20, 30]}]}}
    if adj:
        res["indicators"]["adjclose"] = [{"adjclose": [50, 50, 52]}]  # a 2:1 split-like adjustment on day 1
    return {"chart": {"result": [res], "error": None}}


def test_yahoo_parses_and_adjusts():
    c, h = _client(_payload())
    df = YahooProvider(c).bars("BTC/USD", "2024-01-01", "2024-01-31")
    assert "BTC-USD" in str(h.last.url)  # crypto symbol mapped
    # crypto bars are relabelled to the date the close became known (UTC bar day + 1): no lookahead
    assert list(df.index.strftime("%Y-%m-%d")) == ["2024-01-03", "2024-01-04", "2024-01-05"]
    assert list(df["close"]) == [50, 50, 52] and df["open"].iloc[0] == 50  # day-1 prices adjusted by adj/close
    assert list(df["volume"]) == [10, 20, 30]


def test_yahoo_without_adjclose_and_with_gaps():
    p = _payload(adj=False)
    p["chart"]["result"][0]["indicators"]["quote"][0]["close"][1] = None
    c, _ = _client(p)
    df = YahooProvider(c).bars("SPY", "2024-01-01", "2024-01-31")
    assert len(df) == 2  # the null close row is dropped


def test_yahoo_error_payload_raises_clearly():
    c, _ = _client({"chart": {"result": None, "error": {"code": "Not Found"}}})
    with pytest.raises(ValueError, match="no data"):
        YahooProvider(c).bars("ZZZZ", "2024-01-01", "2024-01-31")
    c, _ = _client({}, status=429)
    with pytest.raises(httpx.HTTPStatusError):
        YahooProvider(c).bars("SPY", "2024-01-01", "2024-01-31")


def test_yahoo_equity_dates_are_not_shifted():
    c, _ = _client(_payload())
    df = YahooProvider(c).bars("SPY", "2024-01-01", "2024-01-31")
    assert list(df.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03", "2024-01-04"]
