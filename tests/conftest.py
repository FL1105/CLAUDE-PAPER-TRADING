from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from polybot.config import Config, load_config

FIXTURES = Path(__file__).parent / "fixtures"
ASSUMED = FIXTURES / "assumed"
LIVE = FIXTURES / "live"


def load_fixture(name: str, kind: str = "assumed") -> Any:
    with (FIXTURES / kind / f"{name}.json").open() as fh:
        return json.load(fh)


@pytest.fixture
def cfg() -> Config:
    return load_config()
