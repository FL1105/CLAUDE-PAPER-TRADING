from __future__ import annotations

import json
import shutil
import time
from email.utils import formatdate
from pathlib import Path

import httpx

from polybot.config import DEFAULT_CONFIG_PATH, config_hash, load_config
from polybot.data.store import Store
from polybot.doctor import FAIL, OK, WARN, render, run

ROOT = Path(__file__).resolve().parents[1]


def _root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "config" / "default.yaml")
    return tmp_path


def by(checks: list) -> dict:  # type: ignore[type-arg]
    return {c.name: c for c in checks}


def test_offline_fresh_machine(tmp_path: Path) -> None:
    root = _root(tmp_path)
    c = by(run(root, root / "config" / "default.yaml", offline=True))
    assert c["Python"].status == OK and c["Required packages"].status == OK
    assert c["Output folder + SQLite"].status == OK
    assert c["API probe"].status == WARN and "polybot probe" in c["API probe"].fix
    assert c["Database"].status == WARN
    text = render(list(c.values()))
    assert text.isascii() and "warning(s)" in text


def test_network_ok_blocked_and_clock(tmp_path: Path) -> None:
    root = _root(tmp_path)
    skewed = formatdate(time.time() - 30, usegmt=True)

    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"date": skewed})

    c = by(run(root, root / "config" / "default.yaml", transport=httpx.MockTransport(ok)))
    assert c["Gamma API (markets)"].status == OK and c["CLOB API (books)"].status == OK
    assert c["Clock"].status == WARN  # 30 s off

    def blocked(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    c = by(run(root, root / "config" / "default.yaml", transport=httpx.MockTransport(blocked)))
    assert c["Data API (trades)"].status == FAIL and "refused" in c["Data API (trades)"].detail

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("403 Forbidden", request=req)

    c = by(run(root, root / "config" / "default.yaml", transport=httpx.MockTransport(down)))
    assert c["Gamma API (markets)"].status == FAIL and "proxy" in c["Gamma API (markets)"].detail


def test_forward_run_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = load_config(root / "config" / "default.yaml")
    with Store(root / cfg.storage.db_path) as st:
        st.set_meta("forward_start_ts", str(int(time.time()) - 3 * 86_400))
        st.set_meta("forward_config_hash", "something-else")
        st.set_meta("heartbeat", json.dumps({"ts": int(time.time()) - 3600}))
        st.set_halt("drawdown")
    c = by(run(root, root / "config" / "default.yaml", offline=True))
    assert c["Forward run"].status == OK and "3.0 days" in c["Forward run"].detail
    assert c["Config unchanged since start"].status == FAIL
    assert c["Heartbeat"].status == WARN and "not running" in c["Heartbeat"].fix
    assert c["Kill switch"].status == WARN
    assert config_hash(cfg)


def test_bad_config(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("nope: [", encoding="utf-8")
    c = by(run(tmp_path, bad, offline=True))
    assert c["Config"].status == FAIL


def test_windows_scripts_are_ascii() -> None:
    for p in [ROOT / "setup.ps1", *(ROOT / "scripts" / "windows").glob("*.ps1")]:
        assert p.read_bytes().isascii(), p  # Windows PowerShell 5.1 misreads non-ASCII
