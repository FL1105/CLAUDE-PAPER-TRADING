"""Safety by absence: no real-money order path, no key handling beyond paper env vars."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def _py():
    return [p for p in SRC.rglob("*.py")]


def test_no_live_trading_hosts_or_flags():
    bad = []
    for p in _py():
        t = p.read_text(encoding="utf-8")
        if re.search(r"(?<!paper-)api\.alpaca\.markets", t):
            bad.append((p.name, "non-paper alpaca trading host"))
        if re.search(r"LIVE_ENABLED\s*=\s*True|live_enabled\s*=\s*True", t):
            bad.append((p.name, "live flag enabled"))
        if re.search(r"private_key|PRIVATE_KEY|eth_account|web3|sign_order", t):
            bad.append((p.name, "key/signing code"))
    assert not bad, bad


def test_live_broker_only_in_stub():
    for p in _py():
        if p.name == "live_stub.py":
            continue
        assert "LiveBroker(" not in p.read_text(encoding="utf-8"), p


def test_secrets_only_from_env_and_gitignored():
    gi = (SRC.parent / ".gitignore").read_text()
    assert ".env" in gi
    for p in _py():
        t = p.read_text(encoding="utf-8")
        assert not re.search(r"(secret|key_id)\s*=\s*[\"'][A-Za-z0-9]{12,}[\"']", t, re.I), p


def test_data_dir_and_runs_ignored():
    gi = (SRC.parent / ".gitignore").read_text()
    assert "data/" in gi and "runs/" in gi
