"""A simple paper bot that learns which rule (if any) beats real costs.

Design (the simplest honest version of "learns"):
    * Market: Polymarket BTC 5-minute Up/Down windows. One resolves every 5 minutes, so feedback
      is fast. Prices are REAL asks recorded by ``polybot.btc5`` (with lag and the documented fee).
    * Arms: a handful of simple rules ("always Up", "favourite", "follow momentum", ...) each
      evaluated at two decision times. Every arm paper-trades EVERY window, so exploring costs
      nothing and no arm is starved of data.
    * Learning: per-arm mean net return per dollar with a standard error. The bot's own choice each
      window is Thompson sampling over the arms plus a "sit out" arm worth exactly 0, using only
      past windows (no look-ahead). Early on the posteriors are wide, so it mostly sits out.
    * Safety: an arm is only called "proven" when its lower confidence bound is above 0 after a
      Bonferroni correction for the number of arms AND it has at least ``min_trades`` windows.
      With no real edge, nothing is proven; that is the correct answer, not a failure.

Paper only. It never places an order.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scipy.stats import norm

from polybot.btc5 import open_db

OFFSETS = (240.0, 120.0)  # seconds before the window ends at which an arm decides
LAG_S = 2.0
MAX_STALE_S = 6.0
FEE_RATE = 0.07  # documented crypto taker feeRate: fee = rate * p * (1 - p) per share
MIN_TRADES = 100
ALPHA = 0.05
PRIOR_SD = 0.5  # return sd assumed before an arm has data

SCHEMA = """
CREATE TABLE IF NOT EXISTS learner_arm (
  arm TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0,
  sum_r REAL NOT NULL DEFAULT 0, sum_r2 REAL NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS learner_log (
  start_ts INTEGER PRIMARY KEY, chosen TEXT, bot_return REAL, bankroll REAL NOT NULL);
"""


@dataclass(frozen=True)
class Feat:
    up_mid: float
    up_mid_prev: float | None  # mid about 60 s earlier
    spread: float


Rule = Callable[[Feat], str | None]  # returns "UP", "DOWN" or None


def _fav(f: Feat) -> str:
    return "UP" if f.up_mid >= 0.5 else "DOWN"


RULES: dict[str, Rule] = {
    "always_up": lambda f: "UP",
    "always_down": lambda f: "DOWN",
    "favourite": _fav,
    "underdog": lambda f: "DOWN" if _fav(f) == "UP" else "UP",
    "follow_momentum": lambda f: (
        None if f.up_mid_prev is None else ("UP" if f.up_mid > f.up_mid_prev else "DOWN")
    ),
    "fade_momentum": lambda f: (
        None if f.up_mid_prev is None else ("DOWN" if f.up_mid > f.up_mid_prev else "UP")
    ),
}
ARMS = [f"{r}@{int(o)}s" for r in RULES for o in OFFSETS]


def _snap(con: sqlite3.Connection, start: int, t: float) -> tuple[float, float, float] | None:
    r = con.execute(
        "SELECT ts, bid, ask FROM btc5_books WHERE start_ts=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (start, t),
    ).fetchone()
    if r is None or t - r[0] > MAX_STALE_S or r[1] is None or r[2] is None:
        return None
    return r[0], r[1], r[2]


def window_returns(
    con: sqlite3.Connection, start: int, end: int, up_won: bool
) -> dict[str, float | None]:
    """Net return per dollar of every arm in one window (None = could not trade at a real ask)."""
    out: dict[str, float | None] = {a: None for a in ARMS}
    for off in OFFSETS:
        td = end - off
        now = _snap(con, start, td)
        if now is None:
            continue
        prev = _snap(con, start, td - 60.0)
        feat = Feat(
            (now[1] + now[2]) / 2,
            None if prev is None else (prev[1] + prev[2]) / 2,
            now[2] - now[1],
        )
        fill = con.execute(
            "SELECT bid, ask FROM btc5_books "
            "WHERE start_ts=? AND ts>=? AND ts<? ORDER BY ts LIMIT 1",
            (start, td + LAG_S, end),
        ).fetchone()
        if fill is None or fill[0] is None or fill[1] is None:
            continue
        for name, rule in RULES.items():
            side = rule(feat)
            if side is None:
                continue
            price = fill[1] if side == "UP" else 1.0 - fill[0]
            if not 0.0 < price < 1.0:
                continue
            fee = FEE_RATE * price * (1 - price)
            win = up_won if side == "UP" else not up_won
            out[f"{name}@{int(off)}s"] = ((1.0 if win else 0.0) - price - fee) / (price + fee)
    return out


@dataclass
class ArmStat:
    n: int = 0
    sum_r: float = 0.0
    sum_r2: float = 0.0
    wins: int = 0

    @property
    def mean(self) -> float:
        return self.sum_r / self.n if self.n else 0.0

    @property
    def sd(self) -> float:
        if self.n < 2:
            return PRIOR_SD
        var = max(0.0, (self.sum_r2 - self.n * self.mean**2) / (self.n - 1))
        return math.sqrt(var) or PRIOR_SD

    @property
    def se(self) -> float:
        return self.sd / math.sqrt(self.n) if self.n else PRIOR_SD

    def lower(self, n_arms: int) -> float:
        z = float(norm.ppf(1 - ALPHA / max(1, n_arms)))
        return self.mean - z * self.se


def load_stats(con: sqlite3.Connection) -> dict[str, ArmStat]:
    st = {a: ArmStat() for a in ARMS}
    for arm, n, s, s2, w in con.execute("SELECT arm, n, sum_r, sum_r2, wins FROM learner_arm"):
        if arm in st:
            st[arm] = ArmStat(n, s, s2, w)
    return st


def choose_arm(stats: dict[str, ArmStat], rng: random.Random) -> str | None:
    """Thompson sampling over arms plus a 'sit out' option worth 0."""
    best, best_v = None, 0.0
    for arm, s in stats.items():
        v = rng.gauss(s.mean, s.se)
        if v > best_v:
            best, best_v = arm, v
    return best


def learn(db: Path, *, seed: int = 0, stake_frac: float = 0.01, bankroll0: float = 1000.0) -> int:
    """Process every resolved window not yet learned from, in time order. Returns windows done."""
    con = open_db(db)
    con.executescript(SCHEMA)
    rng = random.Random(seed)
    stats = load_stats(con)
    mx = con.execute("SELECT MAX(start_ts) FROM learner_log").fetchone()[0]
    last = -1 if mx is None else int(mx)
    row = con.execute("SELECT bankroll FROM learner_log ORDER BY start_ts DESC LIMIT 1").fetchone()
    bankroll = float(row[0]) if row else bankroll0
    rng.seed(seed + last)  # a different but reproducible stream after every restart
    done = 0
    for start, end, up_won in con.execute(
        "SELECT start_ts, end_ts, up_won FROM btc5_windows WHERE up_won IS NOT NULL "
        "AND start_ts > ? ORDER BY start_ts",
        (last,),
    ).fetchall():
        rets = window_returns(con, start, end, bool(up_won))
        chosen = choose_arm(stats, rng)  # decided from PAST windows only
        r_bot = None
        if chosen is not None and rets.get(chosen) is not None:
            r_bot = rets[chosen]
            bankroll += bankroll * stake_frac * r_bot
        for arm, r in rets.items():
            if r is None:
                continue
            s = stats[arm]
            s.n += 1
            s.sum_r += r
            s.sum_r2 += r * r
            s.wins += r > 0
        con.execute(
            "INSERT OR REPLACE INTO learner_log VALUES (?,?,?,?)", (start, chosen, r_bot, bankroll)
        )
        done += 1
    for arm, s in stats.items():
        con.execute(
            "INSERT OR REPLACE INTO learner_arm VALUES (?,?,?,?,?)",
            (arm, s.n, s.sum_r, s.sum_r2, s.wins),
        )
    con.commit()
    con.close()
    return done


def report(db: Path) -> dict[str, Any]:
    con = open_db(db)
    con.executescript(SCHEMA)
    stats = load_stats(con)
    k = len(stats)
    arms = []
    for arm, s in stats.items():
        arms.append(
            {
                "arm": arm,
                "windows": s.n,
                "win_rate": s.wins / s.n if s.n else None,
                "mean_net_return": s.mean if s.n else None,
                "lower_bound": s.lower(k) if s.n else None,
                "proven": s.n >= MIN_TRADES and s.lower(k) > 0,
            }
        )
    arms.sort(key=lambda a: -(a["lower_bound"] if a["lower_bound"] is not None else -9))
    log = con.execute(
        "SELECT COUNT(*), SUM(chosen IS NOT NULL AND bot_return IS NOT NULL), "
        "(SELECT bankroll FROM learner_log ORDER BY start_ts DESC LIMIT 1) FROM learner_log"
    ).fetchone()
    con.close()
    return {
        "windows_learned": log[0],
        "bot_trades": int(log[1] or 0),
        "bot_bankroll": log[2],
        "arms": arms,
        "proven_arms": [a["arm"] for a in arms if a["proven"]],
    }
