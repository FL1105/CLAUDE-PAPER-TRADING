"""Alpaca PAPER probe: checks that the endpoints and field names this bot relies on are real.

Read-only by default (GET requests only). `roundtrip=True` additionally places ONE tiny PAPER order and
cancels or sells it, to verify order formatting end to end. Output is redacted (no account ids, no keys) so it
can be pasted back for review. Everything it checks is also in docs/SOURCES.md as UNVERIFIED."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from bot.broker.alpaca_paper import DATA_BASE, PAPER_BASE, AlpacaPaperBroker, tick_round
from bot.broker.base import Order

REDACT = {"id", "account_number", "client_order_id", "created_at", "updated_at", "submitted_at", "filled_at",
          "expired_at", "canceled_at", "failed_at", "replaced_at", "asset_id", "user_id"}


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in REDACT else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj[:3]]
    return obj


def _check(name: str, r: httpx.Response, expect_keys: list[str], results: list[dict], sample_path=None) -> dict | None:
    item = {"check": name, "status_code": r.status_code, "ok": False, "missing_keys": [], "note": ""}
    try:
        js = r.json()
    except ValueError:
        js = None
        item["note"] = f"non-JSON body: {r.text[:120]}"
    if r.status_code == 200 and js is not None:
        node = js
        for part in sample_path or []:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, list):
            node = node[0] if node else {}
        if isinstance(node, dict):
            item["missing_keys"] = [k for k in expect_keys if k not in node]
            item["ok"] = not item["missing_keys"]
            item["keys_seen"] = sorted(node)[:40]
            item["sample"] = _redact(node)
        else:
            item["note"] = f"expected an object at {sample_path}, got {type(node).__name__}"
    elif r.status_code != 200:
        item["note"] = f"HTTP {r.status_code}: {r.text[:160]}"
    results.append(item)
    return js


def run_probe(key_id: str, secret: str, roundtrip: bool = False, client: httpx.Client | None = None,
              poll_seconds: float = 45.0, sleep=time.sleep) -> dict:
    c = client or httpx.Client(headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}, timeout=30)
    res: list[dict] = []
    get = lambda url, **kw: c.get(url, **kw)  # noqa: E731
    acct = _check("account", get(f"{PAPER_BASE}/v2/account"), ["equity", "cash", "status"], res)
    _check("clock", get(f"{PAPER_BASE}/v2/clock"), ["is_open", "next_open", "next_close"], res)
    _check("positions", get(f"{PAPER_BASE}/v2/positions"), ["symbol", "qty"], res)
    _check("asset SPY", get(f"{PAPER_BASE}/v2/assets/SPY"), ["tradable", "fractionable", "status"], res)
    _check("asset BIL", get(f"{PAPER_BASE}/v2/assets/BIL"), ["tradable", "fractionable", "status"], res)
    _check("asset BTC/USD", get(f"{PAPER_BASE}/v2/assets/BTC%2FUSD"), ["tradable", "status"], res)
    _check("stock bars SPY", get(f"{DATA_BASE}/v2/stocks/SPY/bars", params={
        "timeframe": "1Day", "limit": 3, "adjustment": "all", "feed": "iex"}), ["t", "o", "h", "l", "c", "v"],
        res, ["bars"])
    last = _check("latest trade SPY", get(f"{DATA_BASE}/v2/stocks/SPY/trades/latest", params={"feed": "iex"}),
                  ["p"], res, ["trade"])
    _check("crypto bars BTC/USD", get(f"{DATA_BASE}/v1beta3/crypto/us/bars", params={
        "symbols": "BTC/USD", "timeframe": "1Day", "limit": 3}), ["t", "o", "h", "l", "c", "v"], res,
        ["bars", "BTC/USD"])
    _check("crypto latest trade", get(f"{DATA_BASE}/v1beta3/crypto/us/latest/trades", params={"symbols": "BTC/USD"}),
           ["p"], res, ["trades", "BTC/USD"])
    out = {"checks": res, "roundtrip": None}
    if acct:
        out["account_flags"] = {k: acct.get(k) for k in ("status", "trading_blocked", "account_blocked",
                                                         "pattern_day_trader", "multiplier", "shorting_enabled",
                                                         "crypto_status") if k in acct}
    if roundtrip and all(x["ok"] for x in res[:1]) and last:
        out["roundtrip"] = _roundtrip(c, (last.get("trade") or {}).get("p"), poll_seconds, sleep)
    out["summary"] = {"passed": sum(1 for x in res if x["ok"]), "failed": [x["check"] for x in res if not x["ok"]]}
    return out


def _roundtrip(c: httpx.Client, last_price, poll_seconds: float, sleep) -> dict:
    """One share of SPY: marketable limit buy, wait, then sell it back (or cancel it if the market is closed)."""
    steps: list[dict] = []
    if not last_price:
        return {"steps": [{"step": "skipped", "note": "no last price"}]}
    b = AlpacaPaperBroker(client=c)
    cid = f"probe-{int(time.time())}"
    buy = Order(f"{cid}-buy", "SPY", "buy", 1.0, tick_round(float(last_price) * 1.02, "buy"))
    f = b.submit_order(buy)
    steps.append({"step": "buy submitted", "status": f.status, "reason": f.reason})
    deadline = time.time() + poll_seconds
    while f.status in ("open",) and time.time() < deadline:
        sleep(3)
        f = b.get_order(buy.client_order_id) or f
    steps.append({"step": "buy after wait", "status": f.status, "qty": f.qty, "price": f.price})
    if f.status in ("filled", "partial") and f.qty > 0:
        sell = Order(f"{cid}-sell", "SPY", "sell", f.qty, tick_round(float(last_price) * 0.98, "sell"))
        g = b.submit_order(sell)
        steps.append({"step": "sell submitted", "status": g.status, "reason": g.reason})
        deadline = time.time() + poll_seconds
        while g.status == "open" and time.time() < deadline:
            sleep(3)
            g = b.get_order(sell.client_order_id) or g
        steps.append({"step": "sell after wait", "status": g.status, "qty": g.qty, "price": g.price})
    else:
        steps.append({"step": "cancel", "cancelled": b.cancel(buy.client_order_id),
                      "note": "market closed or order not filled: accepted-and-cancel still proves formatting"})
    return {"steps": steps}


def write_probe(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


def render(result: dict) -> str:
    lines = []
    for x in result["checks"]:
        flag = "OK  " if x["ok"] else "FAIL"
        extra = f" missing {x['missing_keys']}" if x["missing_keys"] else (f" {x['note']}" if x["note"] else "")
        lines.append(f"{flag} {x['check']} (HTTP {x['status_code']}){extra}")
    if result.get("account_flags"):
        lines.append(f"account: {result['account_flags']}")
    if result.get("roundtrip"):
        for s in result["roundtrip"]["steps"]:
            lines.append(f"round trip: {s}")
    return "\n".join(lines)
