from __future__ import annotations

from polybot.config import Config
from polybot.data.synthetic import DAY, T0, generate_world
from polybot.execution.paper import PaperState
from polybot.selection.walkforward import DataView, Epoch
from polybot.strategies.base import MarketEvent
from polybot.strategies.wallet_copy import WalletCopy

EP = Epoch(0, 1000, 2000)
STATE = PaperState(bankroll0=1000, cash=1000)


def ev(wallet: str = "w1", side: str = "BUY", price: float = 0.5, ts: int = 1500) -> MarketEvent:
    return MarketEvent(
        ts=ts,
        wallet=wallet,
        condition_id="c",
        token_id="t",
        side=side,
        price=price,
        size_shares=10,
        trade_key=f"{wallet}{side}{price}{ts}",
    )


def test_copies_only_frozen_wallets_in_band_and_epoch(cfg: Config) -> None:
    s = WalletCopy(cfg, 0)
    s.set_frozen(frozenset({"w1"}), EP)
    assert len(s.on_event(ev(), STATE)) == 1
    assert s.on_event(ev("other"), STATE) == []
    assert s.on_event(ev(price=0.97), STATE) == []  # penny-picking entry
    assert s.on_event(ev(price=0.02), STATE) == []
    assert s.on_event(ev(ts=2000), STATE) == []  # outside the selection's epoch
    exit_sig = s.on_event(ev(side="SELL", price=0.97), STATE)
    assert exit_sig[0].kind == "info_exit"
    entry = s.on_event(ev(), STATE)[0]
    assert entry.wallet_price == 0.5 and entry.strategy == "wallet_copy"


def test_epoch_start_runs_leakage_safe_selection(cfg: Config) -> None:
    sc = cfg.synthetic.model_copy(
        update={"n_markets": 600, "n_wallets": 300, "calibration_mc_sims": 200}
    )
    w = generate_world(sc, 4, "null")
    start = T0 + 90 * DAY
    s = WalletCopy(cfg, 0)
    s.on_epoch_start(Epoch(0, start, start + 14 * DAY), DataView(w.trades, w.wf_markets, start))
    assert isinstance(s.frozen, frozenset)
    assert "markets_in_window" in s.last_diagnostics
