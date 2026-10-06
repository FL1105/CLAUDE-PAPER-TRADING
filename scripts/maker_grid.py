#!/usr/bin/env python3
"""Replay the conservative passive maker over a recorded DB for a grid of parameters.

Usage: python scripts/maker_grid.py out/maker/long.sqlite > out/overnight/maker_grid_long.json
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

from polybot.maker import MakerParams, simulate

db = Path(sys.argv[1])
rows = []
for spread, quote, inv in itertools.product((0.005, 0.01, 0.02), (5.0, 20.0), (25.0, 100.0)):
    s = simulate(
        db, MakerParams(quote_usd=quote, max_inventory_usd=inv, min_spread=spread)
    ).summary()
    rows.append({"min_spread": spread, "quote_usd": quote, "max_inv_usd": inv, **s})
print(json.dumps(rows, indent=1))
