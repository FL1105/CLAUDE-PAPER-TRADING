#!/usr/bin/env python3
"""Phase 5 checkpoint: full-pipeline false-promotion rate and power on synthetic worlds.

Writes out/acceptance_<name>.json. Usage: python scripts/phase5_acceptance.py [--quick]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from polybot.config import load_config
from polybot.experiments import acceptance

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    quick = "--quick" in sys.argv
    cfg = load_config()
    strong = cfg.model_copy(
        update={
            "synthetic": cfg.synthetic.model_copy(
                update={"sigma_noise": 0.15, "skilled_scan_multiplier": 8.0}
            )
        }
    )
    tight = strong.model_copy(
        update={"execution": strong.execution.model_copy(update={"sim_half_spread": 0.005})}
    )
    n_null = 20 if quick else cfg.synthetic.acceptance_null_seeds
    n_sk = 10 if quick else 100
    runs = [
        ("null_calibrated", cfg, "null", 0.1, None, n_null),
        ("null_additive", cfg, "null", 0.1, "additive", n_null),
        ("skilled_default_lowlat", cfg, "skilled", 0.1, None, n_sk),
        ("skilled_strong_lowlat", strong, "skilled", 0.1, None, n_sk),
        ("skilled_strong_lowlat_halfspread_0.005", tight, "skilled", 0.1, None, n_sk),
        ("skilled_strong_highlat", strong, "skilled", 5.0, None, n_sk),
    ]
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    workers = os.cpu_count() or 1
    for name, c, world, ratio, pm, n in runs:
        t = time.time()
        r = acceptance(
            c,
            [cfg.seed + 10_000 + i for i in range(n)],
            world,
            ratio,
            pm,  # type: ignore[arg-type]
            workers=workers,
        )
        r["elapsed_s"] = round(time.time() - t, 1)
        (out / f"acceptance_{name}.json").write_text(
            json.dumps(r, indent=1, sort_keys=True), encoding="utf-8"
        )
        print(name, json.dumps({k: v for k, v in r.items() if k != "runs"}), flush=True)


if __name__ == "__main__":  # required on Windows (spawned worker processes)
    main()
