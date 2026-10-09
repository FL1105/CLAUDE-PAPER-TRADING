"""Dashboard API (FastAPI). Binds to localhost only (see cli `dash`). Reads are open on localhost;
the two write actions only REDUCE risk and require the local token in `X-Local-Token`."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from bot.config import ROOT
from bot.dashboard import data
from bot.risk.killswitch import KillSwitch

STATIC = Path(__file__).parent / "static"


def ensure_token(root: Path) -> str:
    p = root / "state" / "dash_token"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def create_app(root: Path | None = None) -> FastAPI:
    root = Path(root or ROOT)
    token = ensure_token(root)
    app = FastAPI(title="CompoundBot dashboard", docs_url=None, redoc_url=None)

    def auth(t: str | None) -> None:
        if not t or not secrets.compare_digest(t, token):
            raise HTTPException(status_code=403, detail="bad or missing X-Local-Token (state/dash_token)")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/summary")
    def summary():
        return data.summary(root)

    @app.get("/api/equity")
    def equity():
        return data.equity(root)

    @app.get("/api/weights")
    def weights():
        return data.weights(root)

    @app.get("/api/trades")
    def trades(q: str = Query("", max_length=60)):
        return data.trades(root, q)

    @app.get("/api/gate")
    def gate():
        return data.gate(root)

    @app.post("/api/kill")
    def kill(x_local_token: str | None = Header(default=None)):
        auth(x_local_token)
        KillSwitch(root / "state" / "KILL").engage("dashboard button", by="dashboard")
        return {"ok": True}

    @app.post("/api/pause")
    def pause(x_local_token: str | None = Header(default=None)):
        auth(x_local_token)
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "state" / "PAUSE_ENTRIES").write_text("paused via dashboard", encoding="utf-8")
        return {"ok": True}

    @app.get("/api/export.csv")
    def export():
        rows = data.trades(root, "", 100000)
        cols = ["date", "symbol", "side", "qty", "price", "cost_usd", "status", "client_order_id"]
        text = ",".join(cols) + "\n" + "\n".join(",".join(str(r.get(c, "")) for c in cols) for r in rows)
        return PlainTextResponse(text, media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=trades.csv"})

    # No endpoint exists that changes risk limits, selects strategies or enables live trading.
    return app

