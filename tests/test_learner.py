"""The learner must find a real edge when one exists and prove nothing when there is none."""

from __future__ import annotations

import random
from pathlib import Path

from polybot.btc5 import open_db
from polybot.learner import learn, report


def _make(db: Path, n: int, edge: bool, seed: int = 1) -> None:
    """Synthetic BTC windows. The Up mid is 0.60 at the start and moves to 0.64 (rising) or 0.56
    (falling); the favourite is Up either way. With edge=True, Up wins 80% after a rise and 20%
    after a fall, so only a momentum rule has an edge; otherwise prices are calibrated: Up wins with probability equal to the Up mid."""
    rng = random.Random(seed)
    con = open_db(db)
    for i in range(n):
        start = i * 300
        end = start + 300
        rising = rng.random() < 0.5
        now_mid = 0.64 if rising else 0.56
        up_won = (rng.random() < (0.8 if rising else 0.2)) if edge else (rng.random() < now_mid)
        con.execute(
            "INSERT INTO btc5_windows VALUES (?,?,?,?,?)", (start, end, "T", "C", int(up_won))
        )
        for t in range(start, end, 2):
            mid = 0.60 if t < start + 30 else now_mid
            con.execute(
                "INSERT INTO btc5_books VALUES (?,?,?,?,?,?)",
                (start, float(t), mid - 0.01, 100.0, mid + 0.01, 100.0),
            )
    con.commit()
    con.close()


def test_finds_a_real_edge(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite"
    _make(db, 400, edge=True)
    learn(db, seed=1)
    r = report(db)
    assert "follow_momentum@240s" in r["proven_arms"]
    assert "fade_momentum@240s" not in r["proven_arms"]


def test_proves_nothing_without_an_edge(tmp_path: Path) -> None:
    db = tmp_path / "b.sqlite"
    _make(db, 400, edge=False)
    learn(db, seed=1)
    assert report(db)["proven_arms"] == []


def test_learning_is_incremental_and_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    _make(db, 50, edge=False)
    assert learn(db) == 50
    assert learn(db) == 0  # nothing new
    assert report(db)["windows_learned"] == 50
