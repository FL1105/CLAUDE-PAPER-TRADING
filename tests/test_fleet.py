"""Fleet: candidate loading, locked sections, seeding, income range, page."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from polybot.fleet import income_range, load_candidate, load_candidates, seed_database
from polybot.fleet_view import write_html

ROOT = Path(__file__).resolve().parents[1]


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_shipped_candidates_load_distinct(tmp_path: Path) -> None:
    cands = load_candidates(ROOT / "config" / "candidates", ROOT, tmp_path)
    assert {c.name for c in cands} >= {"baseline", "strict_fdr", "tight_chase"}
    assert len({c.db_path for c in cands}) == len(cands)


@pytest.mark.parametrize("section", ["gates", "kill_switches", "micro_live"])
def test_locked_sections_rejected(tmp_path: Path, section: str) -> None:
    f = _write(tmp_path / "c.yaml", f"name: x\noverrides:\n  {section}: {{}}\n")
    with pytest.raises(ValueError, match="may not override"):
        load_candidate(f, ROOT, tmp_path)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    f = _write(tmp_path / "c.yaml", "name: x\noverrides:\n  stats:\n    bogus: 1\n")
    with pytest.raises(ValueError):
        load_candidate(f, ROOT, tmp_path)


def test_identical_configs_rejected(tmp_path: Path) -> None:
    d = tmp_path / "c"
    d.mkdir()
    _write(d / "a.yaml", "name: a\noverrides: {}\n")
    _write(d / "b.yaml", "name: b\noverrides: {}\n")
    with pytest.raises(ValueError, match="identical"):
        load_candidates(d, ROOT, tmp_path)


def test_seed_copies_once(tmp_path: Path) -> None:
    c = load_candidate(ROOT / "config" / "candidates" / "baseline.yaml", ROOT, tmp_path)
    hist = tmp_path / "hist.sqlite"
    hist.write_bytes(b"one")
    assert seed_database(c, hist) is True
    hist.write_bytes(b"two")
    assert seed_database(c, hist) is False
    assert c.db_path.read_bytes() == b"one"


def test_income_range() -> None:
    assert income_range(np.array([1.0]), 5)["lo"] is None
    r = income_range(np.full(50, 2.0), 10.0)
    assert r["lo"] == r["hi"] == pytest.approx(300.0)
    noisy = income_range(np.random.default_rng(1).normal(0, 5, 40), 10.0)
    assert noisy["lo"] < 0 < noisy["hi"]


def test_html_embeds_data(tmp_path: Path) -> None:
    out = tmp_path / "f.html"
    write_html({"generated_ts": 0, "bankroll": 1000, "candidates": [], "note": "</script>"}, out)
    text = out.read_text(encoding="utf-8")
    assert "polybot fleet" in text and text.count("</script>") == 2
