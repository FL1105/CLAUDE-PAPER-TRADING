"""Compare broker positions with expected positions; flag any unreconciled difference."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Mismatch:
    symbol: str
    expected: float
    actual: float


def reconcile(expected: dict[str, float], actual: dict[str, float], tol: float = 1e-4) -> list[Mismatch]:
    out = []
    for s in sorted(set(expected) | set(actual)):
        e, a = expected.get(s, 0.0), actual.get(s, 0.0)
        if abs(e - a) > tol * max(1.0, abs(e)):
            out.append(Mismatch(s, e, a))
    return out


def expected_after(positions: dict[str, float], fills) -> dict[str, float]:
    """Apply fills (filled/partial qty) to prior positions."""
    out = dict(positions)
    for f in fills:
        if f.status in ("filled", "partial") and f.qty > 0:
            out[f.symbol] = out.get(f.symbol, 0.0) + (f.qty if f.side == "buy" else -f.qty)
    return {s: q for s, q in out.items() if abs(q) > 1e-12}
