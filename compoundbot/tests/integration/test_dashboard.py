import json

import pytest
from fastapi.testclient import TestClient

from bot.dashboard.api import create_app, ensure_token
from bot.risk.killswitch import KillSwitch


@pytest.fixture
def root(tmp_path):
    for i, d in enumerate(["2025-03-03", "2025-03-04"]):
        (tmp_path / "runs" / d).mkdir(parents=True)
        (tmp_path / "runs" / d / "run.json").write_text(json.dumps({
            "run_id": d, "status": "ok", "ladder": "FULL", "equity": 10000 + 50 * i, "tag": "SYNTHETIC",
            "weights": {"SPY": 0.2, "TLT": 0.1}, "notes": [], "mismatches": [], "inputs_hash": "x",
            "fills": [{"symbol": "SPY", "side": "buy", "qty": 1.0, "price": 500.0, "cost_usd": 0.1,
                       "status": "filled", "client_order_id": f"{d}-SPY-buy"}]}))
    return tmp_path


def test_readonly_endpoints(root):
    c = TestClient(create_app(root))
    s = c.get("/api/summary").json()
    assert s["tag"] == "SYNTHETIC" and s["equity"] == 10050 and s["kill_switch"] is None
    assert c.get("/api/equity").json()["equity"] == [10000.0, 10050.0]
    assert c.get("/api/weights").json()["target"]["SPY"] == 0.2
    assert len(c.get("/api/trades?q=spy").json()) == 2 and c.get("/api/trades?q=zzz").json() == []
    assert "SPY" in c.get("/api/export.csv").text
    assert c.get("/").status_code == 200 and "CompoundBot" in c.get("/").text


def test_kill_switch_needs_token_and_works(root):
    c = TestClient(create_app(root))
    assert c.post("/api/kill").status_code == 403
    assert c.post("/api/kill", headers={"X-Local-Token": "wrong"}).status_code == 403
    tok = ensure_token(root)
    assert c.post("/api/kill", headers={"X-Local-Token": tok}).json() == {"ok": True}
    assert KillSwitch(root / "state" / "KILL").is_engaged()
    assert c.get("/api/summary").json()["kill_switch"]["by"] == "dashboard"
    assert c.post("/api/pause", headers={"X-Local-Token": tok}).status_code == 200
    assert c.get("/api/summary").json()["paused"] is True


def test_no_endpoint_can_change_limits_or_enable_live(root):
    app = create_app(root)
    paths = {r.path for r in app.routes}
    assert not any(w in p for p in paths for w in ("risk", "limit", "live", "select", "config"))
    c = TestClient(app)
    for m in ("put", "patch", "delete"):
        assert getattr(c, m)("/api/summary").status_code in (404, 405)
