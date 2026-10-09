"""Markdown reports. Every table carries a data tag (MEASURED / SYNTHETIC) and the cost assumptions."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from bot.backtest import metrics, stats
from bot.backtest.engine import BacktestResult
from bot.config import Costs

BANNER = {
    "SYNTHETIC": ("> **SYNTHETIC DATA.** These numbers come from a seeded simulator. They show that the "
                  "code and statistics work. They say nothing about whether real markets pay.\n"),
    "MEASURED": "",
}


def cost_header(costs: Costs, exec_mode: str) -> str:
    return (f"Cost assumptions (per side, bps): {costs.per_side_bps}; spread/slippage: "
            f"{costs.spread_slippage_bps}; min order {costs.min_order_usd} USD; capital "
            f"{costs.start_capital_usd:.0f} USD; execution `{exec_mode}`. ESTIMATED, not measured.\n")


def _row(name: str, r: pd.Series, cash: pd.Series, n_boot: int = 300, seed: int = 7) -> list[str]:
    s = metrics.summary(r, cash)
    lo, hi = stats.bootstrap_sharpe_ci(r, n_boot=n_boot, seed=seed, cash=cash)
    return [name, f"{s['cagr']:.1%}", f"{s['vol']:.1%}", f"{s['sharpe']:.2f} [{lo:.2f}, {hi:.2f}]",
            f"{s['sortino']:.2f}", f"{s['max_dd']:.1%}", f"{s['worst_year']:.1%}"]


def table(rows: list[list[str]], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


HEAD = ["strategy", "CAGR", "vol", "Sharpe [95% CI]", "Sortino", "max DD", "worst year"]


def baselines_md(base: dict[str, BacktestResult], cash: pd.Series, tag: str, costs: Costs,
                 exec_mode: str, path: Path) -> None:
    rows = [_row(n, b.net, cash) for n, b in base.items()]
    txt = (f"# Baselines ({tag})\n\n{BANNER.get(tag, '')}\n{cost_header(costs, exec_mode)}\n"
           f"Net of costs. Sharpe/Sortino are excess of the cash proxy.\n\n{table(rows, HEAD)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")


def research_md(res: dict, costs: Costs, exec_mode: str, holdout_note: str, path: Path) -> None:
    tag, cash = res["tag"], res["cash"]
    lines = [f"# Research v1 ({tag}): walk-forward out-of-sample\n", BANNER.get(tag, ""),
             cost_header(costs, exec_mode),
             f"Trials counted for deflated Sharpe: {res['n_trials']} "
             f"(Sharpe variance across trials {res['sr_var']:.3f}). {holdout_note}\n",
             "## Verdicts\n"]
    rows = []
    for n, o in res["outcomes"].items():
        rows.append([n, o.verdict, str(o.final_params), f"{metrics.sharpe(o.oos_net, cash):.2f}"])
    lines.append(table(rows, ["strategy", "verdict", "final params (modal)", "OOS net Sharpe"]))
    base_rows = []
    for n, b in res["baselines"].items():
        first = next(iter(res["outcomes"].values())).oos_net.index if res["outcomes"] else b.net.index
        base_rows.append(_row(n, b.net.reindex(first).dropna(), cash))
    oos_rows = [_row(n, o.oos_net, cash) for n, o in res["outcomes"].items()]
    lines += ["## OOS performance vs baselines (same dates)\n", table(oos_rows + base_rows, HEAD)]
    for n, o in res["outcomes"].items():
        cs = _cost_line(o)
        lines.append(f"## {n}: {o.verdict}\n\n{cs}\n")
        lines.append(table(
            [[c["id"], c["desc"], str(c["value"]), str(c["threshold"]), "PASS" if c["pass"] else "FAIL"]
             for c in o.criteria], ["gate", "criterion", "value", "threshold", "result"]))
        lines.append("Parameter choices by window: " + "; ".join(
            f"{c['from']}: {c['params']}" for c in o.chosen) + "\n")
    lines.append("## WHAT COULD I BE WRONG ABOUT?\n")
    lines += [
        "- ~18 years of data hold only a handful of independent regimes; every test here is low-power.",
        "- Cost inputs are estimates. Real spreads, partial fills and slippage can be worse.",
        "- Parameters were selected walk-forward, but the grid, universe and rules were chosen by people "
        "who already know how markets behaved. That bias is not removable by statistics.",
        "- Total-return adjusted prices may hide data-vendor errors. Run the quality report.",
    ]
    if tag == "SYNTHETIC":
        lines.append("- These are SYNTHETIC numbers: the planted-edge world is far stronger than reality.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cost_line(o) -> str:
    yrs = len(o.oos_net) / 252
    gross_sum, cost_sum = float(o.oos_gross.sum()), float(o.oos_cost.sum())
    n_ord = int(o.oos_orders.sum())
    return (f"OOS: {len(o.oos_net)} days; gross CAGR {metrics.cagr(o.oos_gross):.1%}, net CAGR "
            f"{metrics.cagr(o.oos_net):.1%}; cost drag {cost_sum / yrs:.2%}/yr; orders/yr {n_ord / yrs:.0f}; "
            f"avg net bps/order {o.oos_net.sum() / max(n_ord, 1) * 1e4:.1f}; cost cover "
            f"{(gross_sum / cost_sum) if cost_sum > 0 else float('inf'):.1f}x")
