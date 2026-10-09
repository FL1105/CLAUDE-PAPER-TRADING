"""Replay the last N sessions through the daily runner with the local paper broker (for demos/tests).
Uses whatever is in the data cache; results carry that cache's tag (SYNTHETIC unless you cached real data)."""
import json
import sys

from bot import config as C
from bot.broker.paper_local import PaperLocalBroker
from bot.data import panel as P
from bot.run.daily import run_daily

n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
u, costs = C.load_universe(), C.load_costs()
panel, meta = P.load_cached_panel(u)
broker = PaperLocalBroker(costs, u)
idx = panel["close"].dropna(how="all").index
for d in idx[-n:]:
    res = run_daily(d, panel, broker, tag=meta["tag"])
    print(d.date(), res.status, res.ladder, round(res.equity, 2), len(res.report.fills) if res.report else 0)
(C.ROOT / "state").mkdir(exist_ok=True)
(C.ROOT / "state" / "paper_local.json").write_text(json.dumps(broker.dump()))
