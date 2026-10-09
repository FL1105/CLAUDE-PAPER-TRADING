import pandas as pd
import pytest

from bot.backtest.engine import rebalance_dates, run_backtest
from bot.broker.paper_local import PaperLocalBroker
from bot.config import load_costs, load_risk, load_universe
from bot.data.synthetic import make_prices
from bot.monitor.drift import tracking_error_bps
from bot.risk.killswitch import KillSwitch
from bot.run.daily import compute_targets, run_daily


@pytest.mark.slow
def test_paper_runner_tracks_backtest_replay(tmp_path):
    """Acceptance for gate I2 plumbing: daily tracking error of the runner vs the engine replay < 15 bps."""
    p = make_prices(end="2012-12-31", seed=21)
    w = {k: p[k] for k in ("close", "open", "cash", "volume")}
    u, c, r = load_universe(), load_costs(), load_risk()
    sel = {"trend": {"sma": 100, "vol_window": 60}, "momentum_rotation": {"lookback_months": 9, "top_n": 3},
           "crypto_trend": {"sma": 100, "vol_cap": 1.2}}
    br, ks = PaperLocalBroker(c, u), KillSwitch(tmp_path / "K")
    days = w["close"].index[-60:]
    eq = [run_daily(d, w, br, selected=sel, universe=u, risk=r, costs=c, kill=ks, state_dir=tmp_path / "s",
                    runs_dir=tmp_path / "r").equity for d in days]
    paper = pd.Series(eq, index=days).pct_change().dropna()
    tg, _ = compute_targets(w["close"], sel, u, r)
    rep = run_backtest({**w, "volume": None}, tg, costs=c, risk=r, universe=u,
                       rebalance=rebalance_dates(tg.index, "weekly"), start=days[0])
    te = tracking_error_bps(paper, rep.net.reindex(paper.index))
    assert te < 15, te
