import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from bot import config


def test_all_configs_load():
    u = config.load_universe()
    assert "SPY" in u.symbols and u.cls_of("BTC/USD") == "crypto"
    assert config.load_costs().start_capital_usd > 0
    assert config.load_risk().drawdown_halt_at == 0.15
    assert "trend" in config.load_strategy().model_dump()
    assert config.load_schedule().timezone


def test_rejects_unknown_keys_and_bad_values(tmp_path: Path):
    d = yaml.safe_load((config.CFG / "risk.yaml").read_text())
    bad = dict(d, surprise=1)
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError):
        config.load_risk(p)
    bad = dict(d, gross_exposure_max=1.5)
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError):
        config.load_risk(p)
    bad = dict(d, drawdown_cash_at=0.05)
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError):
        config.load_risk(p)


def test_negative_costs_rejected(tmp_path: Path):
    d = yaml.safe_load((config.CFG / "costs.yaml").read_text())
    d["per_side_bps"]["equity"] = -1
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(d))
    with pytest.raises(ValidationError):
        config.load_costs(p)


def test_risk_hash_detects_change(tmp_path: Path):
    r = tmp_path / "risk.yaml"
    shutil.copy(config.CFG / "risk.yaml", r)
    ack = tmp_path / "ack.txt"
    with pytest.raises(config.RiskHashError):
        config.check_risk_hash(r, ack)  # missing ack
    ack.write_text(config.file_hash(r) + "\n")
    assert config.check_risk_hash(r, ack) == config.file_hash(r)
    r.write_text(r.read_text().replace("0.15", "0.30"))
    with pytest.raises(config.RiskHashError):
        config.check_risk_hash(r, ack)


def test_repo_risk_ack_matches():
    assert config.check_risk_hash()
