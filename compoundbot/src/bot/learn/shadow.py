"""Shadow trading of challengers on data that did not exist when they were chosen.

A challenger is registered with `data_through` = the last bar used in its research. From then on its
hypothetical book (the champion book with that one strategy's parameters swapped) is simulated by the
same causal backtest, and compared with the champion on sessions AFTER `data_through`. No orders are
sent. It is promotable only after `min_shadow_days` new sessions with the paired improvement's 95% CI
lower bound above zero at normal AND doubled costs, with no worse drawdown.
Promotion itself is an operator action (`promote`)."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pandas as pd
import yaml

from bot.backtest import metrics
from bot.backtest.portfolio_report import build_portfolio
from bot.config import ResearchBudget
from bot.monitor.weekly_review import challenger_beats_champion


def load_shadows(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else []


def save_shadows(path: Path, items: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(items, indent=2), encoding="utf-8")


def register(path: Path, strategy: str, params: dict, champion: dict, data_through: str, proposal_id: str,
             today: str | None = None) -> dict:
    items = load_shadows(path)
    sid = f"{strategy}-{proposal_id}-{len(items) + 1}"
    rec = {"id": sid, "strategy": strategy, "params": params, "champion": champion,
           "data_through": data_through, "registered": today or time.strftime("%Y-%m-%d"),
           "proposal": proposal_id, "status": "shadowing"}
    items.append(rec)
    save_shadows(path, items)
    return rec


def evaluate(panel: dict, shadow: dict, selected: dict[str, dict], universe, risk, costs, exec_mode: str,
             budget_cfg: ResearchBudget, tag: str = "MEASURED") -> dict:
    sel_c = {k: dict(v) for k, v in selected.items()}
    sel_h = {k: dict(v) for k, v in selected.items()}
    sel_h[shadow["strategy"]] = dict(shadow["params"])
    start = pd.Timestamp(shadow["data_through"])
    res = {"id": shadow["id"], "data_through": shadow["data_through"]}
    nets = {}
    for mult in (1.0, 2.0):
        c, *_ = build_portfolio(panel, sel_c, universe, risk, costs, exec_mode, tag, cost_multiplier=mult)
        h, *_ = build_portfolio(panel, sel_h, universe, risk, costs, exec_mode, tag, cost_multiplier=mult)
        nets[mult] = (c.net.loc[start + pd.Timedelta(days=1):], h.net.loc[start + pd.Timedelta(days=1):],
                      c.cash_ret)
    c1, h1, cash = nets[1.0]
    n = len(c1)
    res.update({"new_sessions": n, "needed": budget_cfg.min_shadow_days})
    if n < 5:
        res.update({"status": "pending", "promotable": False, "reason": "no new sessions yet"})
        return res
    res.update({"champion_sharpe": metrics.sharpe(c1, cash), "challenger_sharpe": metrics.sharpe(h1, cash),
                "champion_max_dd": metrics.max_drawdown(c1), "challenger_max_dd": metrics.max_drawdown(h1),
                "champion_cagr": metrics.cagr(c1), "challenger_cagr": metrics.cagr(h1)})
    if n < budget_cfg.min_shadow_days:
        res.update({"status": "pending", "promotable": False,
                    "reason": f"{n}/{budget_cfg.min_shadow_days} new sessions"})
        return res
    beats = challenger_beats_champion(c1, h1, nets[2.0][0], nets[2.0][1], n_boot=budget_cfg.shadow_n_boot)
    dd_ok = res["challenger_max_dd"] <= 1.2 * res["champion_max_dd"] + 1e-9
    ok = bool(beats and dd_ok)
    res.update({"status": "promotable" if ok else "rejected", "promotable": ok, "beats_both_costs": bool(beats),
                "drawdown_ok": bool(dd_ok),
                "reason": "improvement CI > 0 at x1 and x2 costs, drawdown ok" if ok else
                          ("improvement not proven at both cost levels" if not beats else "drawdown worse than 1.2x")})
    return res


def promote(shadow_id: str, approved_by: str, shadows_path: Path, selected_path: Path, evaluation: dict,
            backup_dir: Path, log_path: Path) -> dict:
    if not approved_by.strip():
        raise PermissionError("promotion needs an operator name (--approve)")
    if not evaluation.get("promotable"):
        raise PermissionError(f"{shadow_id} is not promotable: {evaluation.get('reason', 'not evaluated')}")
    items = load_shadows(shadows_path)
    sh = next(i for i in items if i["id"] == shadow_id)
    if sh["status"] == "promoted":
        raise PermissionError("already promoted")
    data = yaml.safe_load(Path(selected_path).read_text(encoding="utf-8"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    bk = backup_dir / f"selected_{time.strftime('%Y%m%dT%H%M%S')}.yaml"
    shutil.copy(selected_path, bk)
    old = data["strategies"][sh["strategy"]]
    data["strategies"][sh["strategy"]] = dict(sh["params"])
    data["approved_by"] = f"{approved_by} (promoted {shadow_id})"
    Path(selected_path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    sh["status"] = "promoted"
    save_shadows(shadows_path, items)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "shadow": shadow_id, "by": approved_by, "old": old,
           "new": sh["params"], "backup": str(bk), "evidence": evaluation}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=float) + "\n")
    return rec
