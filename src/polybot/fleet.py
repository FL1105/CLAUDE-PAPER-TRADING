"""Fleet: run several candidate configs side by side and compare them.

Each candidate is a partial override of config/default.yaml with its OWN database
(seeded from the shared backfilled history), so the config-hash rule (gate 12) holds
per candidate. All candidates share one rate-limited API client and are judged by
identical gates.

Assumptions:
    * Candidates may differ only in selection / execution / sizing parameters.
      Overriding ``gates``, ``kill_switches`` or ``micro_live`` is rejected: letting a
      candidate loosen its own bar would defeat the false-promotion control.
    * "Income range" is a bootstrap over settled paper trades scaled to 30 days. It is
      a description of the past evidence, not a forecast.

Failure mode guarded against:
    * Comparing candidates under different bars, or presenting a noisy PnL point
      estimate as expected income: the range is shown, wide when evidence is thin.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import yaml

from polybot.config import DEFAULT_CONFIG_PATH, Config, config_hash

if TYPE_CHECKING:
    from polybot.data.client import PolymarketClient
    from polybot.data.store import Store

LOCKED_SECTIONS = ("gates", "kill_switches", "micro_live", "storage", "api")


@dataclass(frozen=True)
class Candidate:
    name: str
    cfg: Config
    db_path: Path
    out_dir: Path


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_candidate(path: Path, root: Path, fleet_dir: Path) -> Candidate:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict) or not spec.get("name"):
        raise ValueError(f"candidate needs a `name`: {path}")
    name = str(spec["name"])
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"candidate name must be alphanumeric/_/-: {name!r}")
    over = spec.get("overrides") or {}
    locked = [s for s in LOCKED_SECTIONS if s in over]
    if locked:
        raise ValueError(f"candidate {name!r} may not override {locked}")
    base = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    out_dir = fleet_dir / name
    raw = _merge(base, over)
    raw["storage"]["db_path"] = os.path.relpath(out_dir / "polybot.sqlite", root)
    cfg = Config.model_validate(raw)
    return Candidate(name, cfg, root / cfg.storage.db_path, out_dir)


def load_candidates(cand_dir: Path, root: Path, fleet_dir: Path) -> list[Candidate]:
    files = sorted(cand_dir.glob("*.yaml"))
    cands = [load_candidate(f, root, fleet_dir) for f in files]
    hashes = [
        json.dumps(c.cfg.model_dump(mode="json", exclude={"storage"}), sort_keys=True)
        for c in cands
    ]
    names = [c.name for c in cands]
    if len(set(names)) != len(names):
        raise ValueError("duplicate candidate names")
    if len(set(hashes)) != len(hashes):
        raise ValueError("two candidates have identical configs")
    return cands


def seed_database(cand: Candidate, history_db: Path) -> bool:
    """Copy the shared backfilled history into a new candidate DB (never overwrites)."""
    cand.out_dir.mkdir(parents=True, exist_ok=True)
    if cand.db_path.exists() or not history_db.exists():
        return False
    shutil.copyfile(history_db, cand.db_path)
    return True


def income_range(
    pnls: np.ndarray,
    days_observed: float,
    horizon_days: float = 30.0,
    n_boot: int = 2000,
    seed: int = 0,
) -> dict[str, float | None]:
    """5/50/95% bootstrap of paper PnL per `horizon_days` (resampling settled trades)."""
    if len(pnls) < 2 or days_observed <= 0:
        return {"lo": None, "mid": None, "hi": None, "n": len(pnls)}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(pnls), size=(n_boot, len(pnls)))
    tot = pnls[idx].sum(axis=1) * (horizon_days / days_observed)
    lo, mid, hi = np.percentile(tot, [5, 50, 95])
    return {"lo": float(lo), "mid": float(mid), "hi": float(hi), "n": len(pnls)}


def fleet_adjusted_edge(
    store: Store, cfg: Config, n_candidates: int
) -> dict[str, float | bool | int | None]:
    """Gate-3 style net-edge bound at a Bonferroni-widened level.

    Running N candidates and promoting the best is itself a selection step: at a 95%
    one-sided level the chance that at least one of N null candidates passes is ~1-0.95^N.
    Widening the level to ``1 - (1 - ci) / N`` restores the per-gate error rate.
    """
    from polybot.stats.bootstrap import cluster_bootstrap_roi

    f, st = store.fills_frame(), store.settlements_frame()
    base = {"n_candidates": n_candidates, "roi": None, "ci_lower": None, "passes": False}
    if f.empty or st.empty:
        return base
    m = f.merge(st[["fill_id", "net_pnl"]], on="fill_id")
    if m.empty or float(m["stake_usdc"].sum()) <= 0:
        return base
    level = 1.0 - (1.0 - cfg.gates.bootstrap_ci_level) / max(1, n_candidates)
    b = cluster_bootstrap_roi(
        m["net_pnl"],
        m["stake_usdc"],
        m["condition_id"].astype(str),
        n_resamples=cfg.stats.bootstrap_resamples,
        seed=cfg.seed,
        ci_level=level,
    )
    return {
        "n_candidates": n_candidates,
        "level": round(level, 4),
        "roi": float(b.roi),
        "ci_lower": float(b.lower),
        "passes": bool(b.roi > 0 and b.lower > 0),
    }


def summarize(
    cand: Candidate, store: Store, now: int | None = None, n_candidates: int = 1
) -> dict[str, Any]:
    from polybot.report.build import write_outputs

    rep = write_outputs(store, cand.cfg, cand.out_dir, now=now)
    st = store.settlements_frame()
    pnls = st["net_pnl"].to_numpy(dtype=np.float64) if len(st) else np.zeros(0)
    start = store.get_meta("forward_start_ts")
    t = float(now if now is not None else time.time())
    days = max(0.0, (t - float(start)) / 86400.0) if start else 0.0
    lad = rep["ladder"]
    g = cand.cfg.gates
    n_settled = len(pnls)
    rate = n_settled / days if days > 0 else 0.0
    need_trades = max(0, g.min_settled_trades - n_settled)
    eta_trades = need_trades / rate if rate > 0 else (0.0 if need_trades == 0 else None)
    eta_days = None if eta_trades is None else max(eta_trades, max(0.0, g.min_forward_days - days))
    beat = cand.out_dir / "heartbeat.json"
    hb_age = (t - beat.stat().st_mtime) if beat.exists() else None
    return {
        "name": cand.name,
        "config_hash": config_hash(cand.cfg),
        "tier": lad["tier"],
        "next_tier": lad["next_tier"],
        "blocking": [b["name"] for b in lad["blocking_next"]],
        "halted": rep["halted"],
        "days": round(days, 2),
        "summary": rep["summary"],
        "progress": {
            "trades": n_settled,
            "trades_needed": g.min_settled_trades,
            "days_needed": g.min_forward_days,
            "eta_days": None if eta_days is None else round(eta_days, 1),
            "heartbeat_age_s": None if hb_age is None else round(hb_age),
        },
        "income_30d": income_range(pnls, days),
        "fleet_adjusted": fleet_adjusted_edge(store, cand.cfg, n_candidates),
        "equity": rep["equity"],
    }


def _publish(cands: list[Candidate], rows: list[dict[str, Any]], now: int | None) -> dict[str, Any]:
    data = {
        "generated_ts": int(now if now is not None else time.time()),
        "bankroll": cands[0].cfg.paper.bankroll_usdc if cands else None,
        "candidates": rows,
        "note": "Paper results. income_30d is a bootstrap of past settled trades "
        "scaled to 30 days, not a forecast.",
    }
    fleet_dir = cands[0].out_dir.parent
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "fleet.json").write_text(
        json.dumps(data, indent=1, default=str, sort_keys=True), encoding="utf-8"
    )
    return data


def write_fleet(cands: list[Candidate], now: int | None = None) -> dict[str, Any]:
    from polybot.data.store import Store

    rows = []
    for c in cands:
        if c.db_path.exists():
            with Store(c.db_path) as st:
                rows.append(summarize(c, st, now, len(cands)))
    return _publish(cands, rows, now)


def run_fleet(
    cands: list[Candidate],
    client: PolymarketClient,
    *,
    max_iterations: int | None = None,
    report_every_s: float = 300.0,
) -> None:
    """Interleave one `step()` per candidate per poll interval; refresh fleet.json."""
    from polybot.data.store import Store
    from polybot.forward import ForwardLoop

    stores = [Store(c.db_path) for c in cands]
    loops = [ForwardLoop(c.cfg, s, client, c.out_dir) for c, s in zip(cands, stores, strict=True)]
    try:
        for c, lp in zip(cands, loops, strict=True):
            started = lp.store.get_meta("forward_config_hash")
            if started is not None and started != lp.hash:
                raise ValueError(
                    f"candidate {c.name!r} was edited after its forward run began "
                    "(config hash changed). Give the new config a new name (new database)."
                )
            lp.begin()
        last_report = 0.0
        n = 0
        while not any(lp.stop for lp in loops):
            t0 = time.time()
            for lp in loops:
                try:
                    lp.step()
                except Exception as exc:  # one candidate failing must not stop the others
                    lp.stats["errors"] += 1
                    lp.store.log_anomaly("loop_error", repr(exc))
                lp.stats["iterations"] += 1
            if time.time() - last_report >= report_every_s:
                _publish(
                    cands,
                    [summarize(c, s, None, len(cands)) for c, s in zip(cands, stores, strict=True)],
                    None,
                )
                last_report = time.time()
            n += 1
            if max_iterations is not None and n >= max_iterations:
                break
            time.sleep(
                max(0.0, min(c.cfg.paper.poll_interval_s for c in cands) - (time.time() - t0))
            )
        _publish(
            cands,
            [summarize(c, s, None, len(cands)) for c, s in zip(cands, stores, strict=True)],
            None,
        )
    finally:
        for s in stores:
            s.close()
