import httpx
import pytest

from bot.broker.alpaca_paper import AlpacaPaperBroker, NotPaperEndpoint
from bot.broker.base import Order
from bot.broker.live_stub import LiveBroker, NotEnabled


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refuses_non_paper_endpoint():
    with pytest.raises(NotPaperEndpoint):
        AlpacaPaperBroker("k", "s", base_url="https://api.alpaca.markets")
    with pytest.raises(NotPaperEndpoint):
        AlpacaPaperBroker("k", "s", base_url="http://localhost:1234")


def test_requires_keys_without_client(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    with pytest.raises(RuntimeError):
        AlpacaPaperBroker()


def test_account_positions_quote_and_order_flow():
    orders = {}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v2/account":
            return httpx.Response(200, json={"equity": "10000.5", "cash": "4000"})
        if p == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "SPY", "qty": "3.5"}])
        if p == "/v2/stocks/SPY/trades/latest":
            return httpx.Response(200, json={"trade": {"p": 500.25}})
        if p == "/v1beta3/crypto/us/latest/trades":
            return httpx.Response(200, json={"trades": {"BTC/USD": {"p": 60000.0}}})
        if p == "/v2/orders:by_client_order_id":
            cid = req.url.params["client_order_id"]
            return httpx.Response(200, json=orders[cid]) if cid in orders else httpx.Response(404)
        if p == "/v2/orders" and req.method == "POST":
            import json
            body = json.loads(req.content)
            if body["symbol"] == "REJ":
                return httpx.Response(403, text="insufficient buying power")
            o = {"id": "abc", "client_order_id": body["client_order_id"], "symbol": body["symbol"],
                 "side": body["side"], "filled_qty": body["qty"], "filled_avg_price": "500.1",
                 "status": "filled"}
            orders[body["client_order_id"]] = o
            return httpx.Response(200, json=o)
        if p == "/v2/orders/abc" and req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    b = AlpacaPaperBroker(client=_client(handler))
    acct = b.get_account()
    assert acct.equity == 10000.5 and acct.positions == {"SPY": 3.5}
    assert b.get_quote("SPY") == 500.25 and b.get_quote("BTC/USD") == 60000.0
    f = b.submit_order(Order("r1-SPY-buy", "SPY", "buy", 1.5, 501.0))
    assert f.status == "filled" and f.qty == 1.5
    f2 = b.submit_order(Order("r1-SPY-buy", "SPY", "buy", 1.5, 501.0))  # idempotent: no second POST
    assert f2.status == "filled" and len(orders) == 1
    rej = b.submit_order(Order("r1-REJ-buy", "REJ", "buy", 1, 10.0))
    assert rej.status == "rejected" and "403" in rej.reason
    assert b.cancel("r1-SPY-buy") is True and b.get_order("nope") is None


def test_live_broker_is_not_enabled():
    with pytest.raises(NotEnabled):
        LiveBroker()
