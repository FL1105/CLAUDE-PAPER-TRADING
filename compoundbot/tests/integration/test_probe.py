import json

import httpx

from bot.broker import probe


def _good(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p == "/v2/account":
        return httpx.Response(200, json={"id": "SECRET-ID", "account_number": "PA123", "equity": "100000",
                                         "cash": "100000", "status": "ACTIVE", "trading_blocked": False,
                                         "pattern_day_trader": False})
    if p == "/v2/clock":
        return httpx.Response(200, json={"is_open": False, "next_open": "x", "next_close": "y"})
    if p == "/v2/positions":
        return httpx.Response(200, json=[{"symbol": "SPY", "qty": "1"}])
    if p.startswith("/v2/assets/"):
        return httpx.Response(200, json={"id": "asset-uuid", "tradable": True, "fractionable": True,
                                         "status": "active"})
    if p == "/v2/stocks/SPY/bars":
        return httpx.Response(200, json={"bars": [{"t": "2024-01-02T05:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2,
                                                   "v": 3}]})
    if p == "/v2/stocks/SPY/trades/latest":
        return httpx.Response(200, json={"trade": {"p": 500.0}})
    if p == "/v1beta3/crypto/us/bars":
        return httpx.Response(200, json={"bars": {"BTC/USD": [{"t": "2024-01-02T00:00:00Z", "o": 1, "h": 2,
                                                               "l": 1, "c": 2, "v": 3}]}})
    if p == "/v1beta3/crypto/us/latest/trades":
        return httpx.Response(200, json={"trades": {"BTC/USD": {"p": 60000}}})
    if p == "/v2/orders:by_client_order_id":
        cid = req.url.params["client_order_id"]
        found = [o for o in _good.orders if o["client_order_id"] == cid]
        return httpx.Response(200, json=found[0]) if found else httpx.Response(404)
    if p == "/v2/orders" and req.method == "POST":
        b = json.loads(req.content)
        o = {"id": "oid", "client_order_id": b["client_order_id"], "symbol": b["symbol"], "side": b["side"],
             "filled_qty": b["qty"], "filled_avg_price": "500", "status": "filled"}
        _good.orders.append(o)
        return httpx.Response(200, json=o)
    return httpx.Response(404)


_good.orders = []


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_read_only_happy_path_and_redaction():
    res = probe.run_probe("k", "s", client=_client(_good))
    assert res["summary"]["failed"] == [] and res["summary"]["passed"] == 10 and res["roundtrip"] is None
    blob = json.dumps(res)
    assert "SECRET-ID" not in blob and "PA123" not in blob and "asset-uuid" not in blob
    assert "ACTIVE" in probe.render(res) or "OK" in probe.render(res)


def test_probe_flags_missing_fields_and_http_errors():
    def handler(req):
        if req.url.path == "/v2/account":
            return httpx.Response(200, json={"equity": "1"})  # cash and status missing
        if req.url.path == "/v2/clock":
            return httpx.Response(403, text="forbidden")
        return _good(req)

    res = probe.run_probe("k", "s", client=_client(handler))
    bad = {x["check"]: x for x in res["checks"] if not x["ok"]}
    assert set(bad) == {"account", "clock"}
    assert bad["account"]["missing_keys"] == ["cash", "status"] and "403" in bad["clock"]["note"]
    assert "FAIL account" in probe.render(res)


def test_probe_roundtrip_buys_then_sells():
    _good.orders = []
    res = probe.run_probe("k", "s", roundtrip=True, client=_client(_good), poll_seconds=1, sleep=lambda s: None)
    steps = [s["step"] for s in res["roundtrip"]["steps"]]
    assert "buy submitted" in steps and "sell submitted" in steps
    assert {o["side"] for o in _good.orders} == {"buy", "sell"}
