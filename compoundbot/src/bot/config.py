"""Typed configuration. Unknown keys are rejected; risk.yaml is hash-checked."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# BOT_HOME lets demos and tests run in a scratch folder (it must contain config/ and docs/)
ROOT = Path(os.environ.get("BOT_HOME") or Path(__file__).resolve().parents[2])
CFG = ROOT / "config"

CLASSES = {"equity", "bond", "commodity", "reit", "crypto"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Asset(Strict):
    symbol: str
    cls: str
    inception: str

    @field_validator("cls")
    @classmethod
    def _cls(cls, v: str) -> str:
        if v not in CLASSES:
            raise ValueError(f"unknown asset class {v}")
        return v


class CashProxy(Strict):
    symbol: str
    inception: str


class Universe(Strict):
    assets: list[Asset]
    cash_proxy: CashProxy

    @property
    def symbols(self) -> list[str]:
        return [a.symbol for a in self.assets]

    def cls_of(self, symbol: str) -> str:
        return next(a.cls for a in self.assets if a.symbol == symbol)


class Costs(Strict):
    per_side_bps: dict[str, float]
    spread_slippage_bps: dict[str, float]
    commission_usd_per_order: float = Field(ge=0)
    fixed_fee_usd_per_order: float = Field(ge=0)
    min_order_usd: float = Field(ge=0)
    start_capital_usd: float = Field(gt=0)

    @field_validator("per_side_bps", "spread_slippage_bps")
    @classmethod
    def _non_neg(cls, v: dict[str, float]) -> dict[str, float]:
        for k, x in v.items():
            if k not in CLASSES:
                raise ValueError(f"unknown class {k}")
            if x < 0:
                raise ValueError("costs must be >= 0")
        missing = CLASSES - set(v)
        if missing:
            raise ValueError(f"missing classes {sorted(missing)}")
        return v

    def side_bps(self, cls: str, multiplier: float = 1.0) -> float:
        return (self.per_side_bps[cls] + self.spread_slippage_bps[cls]) * multiplier


class Risk(Strict):
    max_weight_per_asset: float = Field(gt=0, le=1)
    crypto_sleeve_max: float = Field(ge=0, le=1)
    gross_exposure_max: float = Field(gt=0, le=1.0)
    target_vol_annual: float = Field(gt=0, le=0.5)
    vol_window: int = Field(ge=20)
    drawdown_halve_at: float = Field(gt=0, lt=1)
    drawdown_cash_at: float = Field(gt=0, lt=1)
    drawdown_halt_at: float = Field(gt=0, lt=1)
    rerisk_recover_within: float = Field(ge=0, lt=1)
    rerisk_min_days: int = Field(ge=1)
    halfrisk_return_days: int = Field(ge=1)
    backtest_halt_rearm_days: int = Field(ge=1)
    daily_loss_halt: float = Field(gt=0, lt=1)
    order_cap_over_max_weight: float = Field(ge=0, lt=1)
    order_cap_adv_frac: float = Field(gt=0, le=1)
    price_collar: float = Field(gt=0, lt=1)
    no_trade_band: float = Field(ge=0, lt=1)

    def model_post_init(self, _ctx: object) -> None:
        if not (self.drawdown_halve_at < self.drawdown_cash_at < self.drawdown_halt_at):
            raise ValueError("drawdown ladder must be increasing: halve < cash < halt")


class Strategy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trend: dict
    momentum_rotation: dict
    mean_reversion: dict
    crypto_trend: dict
    walkforward: dict


class Schedule(Strict):
    timezone: str
    decision_time: str
    heartbeat_file: str
    max_heartbeat_age_hours: float


def _load(name: str, path: Path | None = None) -> dict:
    with open(path or (CFG / name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_universe(path: Path | None = None) -> Universe:
    return Universe(**_load("universe.yaml", path))


def load_costs(path: Path | None = None) -> Costs:
    return Costs(**_load("costs.yaml", path))


def load_risk(path: Path | None = None) -> Risk:
    return Risk(**_load("risk.yaml", path))


def load_strategy(path: Path | None = None) -> Strategy:
    return Strategy(**_load("strategy.yaml", path))


def load_schedule(path: Path | None = None) -> Schedule:
    return Schedule(**_load("schedule.yaml", path))


def file_hash(path: Path) -> str:
    """SHA-256 of the file with normalised line endings (Windows safe)."""
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


class RiskHashError(RuntimeError):
    pass


def check_risk_hash(risk_path: Path | None = None, ack_path: Path | None = None) -> str:
    """Refuse to start paper mode if risk.yaml changed without an operator-signed bump."""
    risk_path = risk_path or (CFG / "risk.yaml")
    ack_path = ack_path or (ROOT / "docs" / "risk_ack.txt")
    current = file_hash(risk_path)
    if not ack_path.exists():
        raise RiskHashError(f"{ack_path} missing. Operator must create it with: {current}")
    text = ack_path.read_text(encoding="utf-8").strip()
    acked = text.split()[0] if text else ""
    if acked != current:
        raise RiskHashError(f"risk.yaml hash {current} does not match acknowledged {acked}")
    return current
