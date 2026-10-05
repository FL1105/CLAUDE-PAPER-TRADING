"""Configuration: pydantic models, YAML loading, validation, freezing and hashing.

Assumptions:
    * Every tunable number in the system lives in ``config/default.yaml`` and is
      loaded through :func:`load_config`. Modules never hard-code thresholds.

Failure mode guarded against:
    * Silent mid-test tuning. Models are frozen (immutable after load), unknown
      keys are rejected (typos cannot silently fall back to a default), and
      :func:`config_hash` gives a stable fingerprint that is stamped on every
      paper fill and report so a changed config is detectable (gate 12).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Prob = Field(ge=0.0, le=1.0)


class ApiConfig(_Frozen):
    gamma_base_url: str
    data_base_url: str
    clob_base_url: str
    rate_limit_per_sec: float = Field(gt=0)
    rate_limit_burst: int = Field(ge=1)
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    backoff_base_s: float = Field(gt=0)
    backoff_max_s: float = Field(gt=0)
    user_agent: str
    cache_dir: str


class StorageConfig(_Frozen):
    db_path: str


class IngestConfig(_Frozen):
    page_limit: int = Field(ge=1)
    max_offset: int = Field(ge=0)
    market_page_limit: int = Field(ge=1)
    market_ids_per_request: int = Field(ge=1)
    forward_overlap_pages: int = Field(ge=0)
    history_import_batch: int = Field(ge=1)


class StatsConfig(_Frozen):
    fdr_q: float = Field(gt=0, lt=1)
    stake_cap_quantile: float = Field(gt=0, le=1)
    alpha: float = Field(gt=0, lt=1)
    power: float = Field(gt=0, lt=1)
    naive_top_n: int = Field(ge=1)
    bootstrap_resamples: int = Field(ge=1000)
    small_n_exact_threshold: int = Field(ge=0)
    selection_mc_sims: int = Field(ge=100)


class FiltersConfig(_Frozen):
    min_resolved_markets: int = Field(ge=1)
    min_active_span_days: float = Field(ge=0)
    max_two_sided_ratio: float = Prob
    max_trades_per_day: float = Field(gt=0)
    max_closed_before_resolution_ratio: float = Prob
    min_entry_price: float = Prob
    max_entry_price: float = Prob
    max_single_market_pnl_share: float = Prob
    flag_wash_trading: bool

    @model_validator(mode="after")
    def _price_band(self) -> FiltersConfig:
        if self.min_entry_price >= self.max_entry_price:
            raise ValueError("min_entry_price must be < max_entry_price")
        return self


class WalkForwardConfig(_Frozen):
    epoch_days: int = Field(ge=1)
    trailing_window_days: int = Field(ge=1)


class PlaceboConfig(_Frozen):
    n_draws: int = Field(ge=1)
    required_percentile: float = Field(gt=0, lt=100)


class ExecutionConfig(_Frozen):
    latency_L_s: float = Field(gt=0)  # noqa: N815  (name mirrors the spec)
    shadow_multipliers: tuple[int, ...]
    drift_half_life_s: float = Field(gt=0)
    max_chase: float = Field(ge=0)
    insufficient_depth: Literal["reject", "partial"]
    default_tick_size: float = Field(gt=0)
    max_median_slippage: float = Field(ge=0)
    share_decimals: int = Field(ge=0, le=6)
    sim_half_spread: float = Field(ge=0)
    sim_levels: int = Field(ge=1)
    sim_level_depth_usdc: float = Field(gt=0)

    @field_validator("shadow_multipliers")
    @classmethod
    def _sorted_positive(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v or any(m <= 0 for m in v) or list(v) != sorted(v):
            raise ValueError("shadow_multipliers must be positive and ascending")
        return v


class FeesConfig(_Frozen):
    formula: Literal["parabolic", "zero"]
    default_coeff: float = Field(ge=0)
    per_category: dict[str, float]
    zero_fee_categories: tuple[str, ...]
    stress_multiplier: float = Field(ge=1)


class PaperConfig(_Frozen):
    bankroll_usdc: float = Field(gt=0)
    stake_frac: float = Field(gt=0, le=1)
    max_market_frac: float = Field(gt=0, le=1)
    max_total_exposure_frac: float = Field(gt=0, le=1)
    max_concurrent_positions: int = Field(ge=1)
    poll_interval_s: float = Field(gt=0)
    heartbeat_interval_s: float = Field(gt=0)


class GatesConfig(_Frozen):
    min_forward_days: int = Field(ge=1)
    min_settled_trades: int = Field(ge=1)
    min_distinct_markets: int = Field(ge=1)
    min_epochs: int = Field(ge=1)
    bootstrap_ci_level: float = Field(gt=0, lt=1)
    persistence_min_pooled_z: float
    persistence_min_positive_frac: float = Prob
    latency_must_survive_multiplier: int = Field(ge=1)
    remove_top_k_trades: int = Field(ge=0)
    max_single_market_pnl_share: float = Prob
    max_single_wallet_pnl_share: float = Prob
    min_fill_rate: float = Prob
    max_drawdown_frac: float = Prob
    losing_streak_percentile: float = Field(gt=0, lt=100)
    min_positive_epoch_frac: float = Prob
    max_epoch_loss_frac: float = Prob
    schema_drift_lookback_days: int = Field(ge=1)


class MicroLiveConfig(_Frozen):
    max_slippage_ratio: float = Field(gt=0)
    min_fill_rate: float = Prob
    min_fills: int = Field(ge=1)
    min_days: int = Field(ge=1)


class KillSwitchConfig(_Frozen):
    max_drawdown_frac: float = Prob
    rolling_window_trades: int = Field(ge=1)
    rolling_roi_percentile: float = Field(gt=0, lt=100)
    slippage_ratio: float = Field(gt=0)
    slippage_consecutive_days: int = Field(ge=1)
    negative_oos_z_consecutive_epochs: int = Field(ge=1)


class SyntheticConfig(_Frozen):
    n_markets: int = Field(ge=1)
    n_wallets: int = Field(ge=1)
    beta_a: float = Field(gt=0)
    beta_b: float = Field(gt=0)
    sigma_noise: float = Field(ge=0)
    skilled_frac: float = Prob
    skilled_obs_noise: float = Field(ge=0)
    skilled_threshold: float = Field(ge=0)
    market_maker_frac: float = Prob
    price_model: Literal["calibrated", "additive"]
    horizon_days: int = Field(ge=2)
    activity_log_mean: float
    activity_log_sd: float = Field(ge=0)
    max_markets_per_wallet: int = Field(ge=1)
    skilled_scan_multiplier: float = Field(ge=1)
    stake_log_mean: float
    stake_log_sd: float = Field(ge=0)
    calibration_mc_sims: int = Field(ge=100)
    acceptance_null_seeds: int = Field(ge=1)
    acceptance_max_false_promotion: float = Prob
    acceptance_min_power: float = Prob


class Config(_Frozen):
    config_version: int = Field(ge=1)
    seed: int
    api: ApiConfig
    storage: StorageConfig
    ingest: IngestConfig
    stats: StatsConfig
    filters: FiltersConfig
    walkforward: WalkForwardConfig
    placebo: PlaceboConfig
    execution: ExecutionConfig
    fees: FeesConfig
    paper: PaperConfig
    gates: GatesConfig
    micro_live: MicroLiveConfig
    kill_switches: KillSwitchConfig
    synthetic: SyntheticConfig


def load_config(path: str | Path | None = None) -> Config:
    """Load, validate and freeze a config file. Raises on any invalid or unknown key."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping: {p}")
    return Config.model_validate(raw)


def config_hash(cfg: Config) -> str:
    """Stable SHA-256 of the validated config (canonical JSON, sorted keys).

    Whitespace / comment / key-order edits in the YAML do not change the hash;
    any value change does.
    """
    canonical = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
