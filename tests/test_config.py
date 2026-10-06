from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from polybot.config import DEFAULT_CONFIG_PATH, Config, config_hash, load_config


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _raw() -> dict:
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def test_default_config_loads(cfg: Config) -> None:
    assert cfg.stats.fdr_q == 0.10
    assert cfg.gates.min_settled_trades == 300
    assert cfg.fees.default_coeff == 0.05
    assert cfg.execution.shadow_multipliers == (1, 2, 5)


def test_config_is_frozen(cfg: Config) -> None:
    with pytest.raises(ValidationError):
        cfg.stats.fdr_q = 0.5  # type: ignore[misc]


def test_unknown_key_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["gates"]["min_setled_trades"] = 1  # typo must not silently default
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw))


def test_invalid_value_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["stats"]["fdr_q"] = 1.5
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw))


def test_price_band_validated(tmp_path: Path) -> None:
    raw = _raw()
    raw["filters"]["min_entry_price"] = 0.95
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw))


def test_hash_stable_across_formatting_and_sensitive_to_values(tmp_path: Path) -> None:
    base = config_hash(load_config())
    raw = _raw()
    # re-serialised YAML (different formatting/comments/order) -> same hash
    assert config_hash(load_config(_write(tmp_path, raw))) == base
    raw["gates"]["min_fill_rate"] = 0.84
    assert config_hash(load_config(_write(tmp_path, raw))) != base


def test_shadow_multipliers_must_ascend(tmp_path: Path) -> None:
    raw = _raw()
    raw["execution"]["shadow_multipliers"] = [5, 1]
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, raw))
