from __future__ import annotations

import json
from pathlib import Path

import httpx

from polybot.config import Config
from polybot.data.client import HttpClient, PolymarketClient
from polybot.data.probe import BEGIN, END, Prober, render_markdown, shape_of, write_assumptions
from tests.conftest import load_fixture

ROUTES = {
    ("gamma-api.polymarket.com", "/markets"): None,  # special-cased (open/closed)
    ("data-api.polymarket.com", "/trades"): "data_trades",
    ("data-api.polymarket.com", "/positions"): "data_positions",
    ("data-api.polymarket.com", "/activity"): "data_activity",
    ("data-api.polymarket.com", "/value"): "data_value",
    ("clob.polymarket.com", "/book"): "clob_book",
    ("clob.polymarket.com", "/price"): "clob_price",
    ("clob.polymarket.com", "/midpoint"): "clob_midpoint",
    ("clob.polymarket.com", "/prices-history"): "clob_prices_history",
}


def fake_api(req: httpx.Request) -> httpx.Response:
    key = (req.url.host, req.url.path)
    if key not in ROUTES:
        return httpx.Response(404, json={"error": "not found"})
    p = req.url.params
    if int(p.get("offset", "0")) > 3000:
        return httpx.Response(400, json={"error": "max offset exceeded"})
    if key[1] == "/markets":
        name = "gamma_markets_closed" if p.get("closed") == "true" else "gamma_markets_open"
        body = load_fixture(name)
    else:
        body = load_fixture(ROUTES[key])  # type: ignore[arg-type]
    if isinstance(body, list) and "limit" in p:
        cap = min(int(p["limit"]), 500)  # pretend the API caps limit at 500
        body = (body * 1000)[:cap]
    return httpx.Response(200, json=body, headers={"X-RateLimit-Remaining": "99"})


def run(cfg: Config, tmp_path: Path):
    http = HttpClient(
        cfg.api, transport=httpx.MockTransport(fake_api), sleep=lambda s: None, seed=0
    )
    return Prober(PolymarketClient(http), tmp_path / "live").run()


def test_probe_end_to_end(cfg: Config, tmp_path: Path) -> None:
    r = run(cfg, tmp_path)
    names = {s.name: s for s in r.steps}
    for n in [
        "gamma_markets_closed",
        "data_trades_global",
        "data_positions",
        "clob_book",
        "clob_prices_history",
    ]:
        assert names[n].ok, n
        assert (tmp_path / "live" / f"{n}.json").exists()
    # unknown endpoint is recorded as a failure, not swallowed
    assert not names["clob_fee_rate"].ok and names["clob_fee_rate"].status == 404
    assert names["data_trades_global"].headers.get("x-ratelimit-remaining") == "99"
    assert all(v.startswith("OK") for v in r.drift.values()), r.drift
    rows = r.pagination["data_trades"]
    assert {"limit": 10_000, "offset": 0, "returned": 500} in rows
    assert any(x.get("offset") == 10_000 and x.get("error") == 400 for x in rows)
    assert r.samples["token_open"] == "5555555555555555555555"


def test_probe_detects_drift(cfg: Config, tmp_path: Path) -> None:
    def drifting(req: httpx.Request) -> httpx.Response:
        resp = fake_api(req)
        if req.url.path == "/trades" and resp.status_code == 200:
            rows = [{k: v for k, v in t.items() if k != "proxyWallet"} for t in resp.json()]
            return httpx.Response(200, json=rows)
        return resp

    http = HttpClient(cfg.api, transport=httpx.MockTransport(drifting), sleep=lambda s: None)
    r = Prober(PolymarketClient(http), tmp_path).run()
    assert r.drift["data_trades_global"].startswith("DRIFT")


def test_markdown_section_replaced_in_place(cfg: Config, tmp_path: Path) -> None:
    r = run(cfg, tmp_path)
    doc = tmp_path / "A.md"
    doc.write_text("# Title\n\nhand-written text\n", encoding="utf-8")
    write_assumptions(doc, render_markdown(r))
    write_assumptions(doc, render_markdown(r))  # idempotent: one section, text preserved
    text = doc.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1 and text.count(END) == 1
    assert "hand-written text" in text and "PARTIAL: " in text  # fee-rate 404s in the fake


def test_shape_of() -> None:
    assert shape_of({"b": [1, 2], "a": "x"}) == {
        "a": "str",
        "b": {"__list_len__": 2, "__item__": "int"},
    }
    json.dumps(shape_of(load_fixture("clob_book")))


def test_cli_probe_unreachable_does_not_write_assumptions(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from polybot import cli
    from polybot.data import client as client_mod

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("blocked", request=req)

    orig = client_mod.HttpClient.__init__

    def patched(self, cfg, **kw):  # type: ignore[no-untyped-def]
        orig(self, cfg, transport=httpx.MockTransport(down), sleep=lambda s: None)

    monkeypatch.setattr(client_mod.HttpClient, "__init__", patched)
    doc = tmp_path / "A.md"
    res = CliRunner().invoke(
        cli.app, ["probe", "--assumptions", str(doc), "--fixtures-dir", str(tmp_path / "live")]
    )
    assert res.exit_code == 1
    assert not doc.exists()
