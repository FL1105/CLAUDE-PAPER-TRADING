"""Hard constraint: v1 contains zero real-money code paths."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from polybot.execution import live

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [
    *(ROOT / "src").rglob("*.py"),
    *(ROOT / "scripts").rglob("*.py"),
    ROOT / "pyproject.toml",
]
FORBIDDEN = [
    r"py[_-]clob[_-]client",
    r"private[_-]?key",
    r"eth[_-]account",
    r"\bweb3\b",
    r"load_dotenv",
    r"\bdotenv\b",
    r"sign_order",
    r"create_order",
    r"post_order",
    r"\.post\(",
    r"\.put\(",
    r"\.delete\(",
    r"\bPOLY_API_KEY\b",
    r"\bmnemonic\b",
]


def test_live_trading_disabled() -> None:
    assert live.LIVE_TRADING_ENABLED is False


def test_live_executor_is_a_stub() -> None:
    with pytest.raises(NotImplementedError):
        live.LiveExecutor()


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_no_real_money_code_paths(pattern: str) -> None:
    hits = [str(p.relative_to(ROOT)) for p in SCANNED if re.search(pattern, p.read_text(), re.I)]
    assert not hits, f"forbidden pattern {pattern!r} found in {hits}"


def test_no_env_files_in_repo() -> None:
    assert not list(ROOT.glob("**/.env"))
