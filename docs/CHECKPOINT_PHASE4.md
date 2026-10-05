# Phase 4 checkpoint: execution realism

## Built
| module | what it does |
|---|---|
| `execution/fees.py` | parabolic fee `coeff * shares * p * (1-p)` per fill level; per-category / zero-fee / stress multiplier |
| `execution/book.py` | walk asks for a USDC stake: merge equal-price levels, snap prices up to the tick, round shares down, chase cap, reject or partial on thin books |
| `execution/latency.py` | drift `p + (q-p)(1-2^(-d/h))` (synthetic), tape reference (real data), synthetic ask ladder |
| `execution/paper.py` | PaperExecutor: risk limits, chase rule, fill, fee, shadow fills at 1x/2x/5x L, settlement (win/lose/void refund), state rebuilt from the ledger after a crash |
| `execution/replay.py` | push walk-forward OOS signals through the executor with delayed prices; time-ordered settlement; summary with fill rate, skip reasons, net ROI and ROI by latency |
| `strategies/base.py`, `wallet_copy.py` | Strategy protocol and Strategy A |
| `data/store.py` | append-only ledger API; signal and fill written in one transaction |

## Property tests (Hypothesis, 300 random books x stakes)
- a fill never beats the best ask
- shares x average price = cost
- cost never exceeds the stake; a complete fill spends the stake up to share rounding
- levels are walked in price order and never over-consumed
- reject mode is all-or-nothing

These tests found a real bug: levels sharing a price were not merged (D23).

## Execution on a clearly-skilled synthetic world (seed 7, 120 days, 8 epochs)

| latency regime | fill rate* | gross at wallet price | **net ROI after spread, depth, fees** | ROI at 1x / 2x / 5x L |
|---|---|---|---|---|
| d/h = 0.07 (fast copier) | 94% | +39.9% | **+29.6%** | 29.6% / 27.7% / 22.6% |
| d/h = 1 | 3.8% | +0.7% | -1.5% | n/a |
| d/h = 5 (edge priced in) | 2.6% | -17.9% | +0.3% | n/a |

\* of signals that reached the book; capacity skips are reported separately (D20).

- Costs take about a quarter of the gross edge even at low latency.
- When latency is comparable to the information half-life, the chase rule rejects
  about 96% of signals. The fill-rate gate (85%) will fail, so edge that only
  exists at zero latency cannot promote.
- **Adverse selection** (D24): at high latency the fills you do get are mostly the
  wallets' wrong calls.

The skilled world here is deliberately strong (sigma_noise 0.15, active skilled
wallets) to exercise the mechanism. It is not an estimate of real-world edge.

## Bugs found and fixed in this phase
1. Equal-price book levels were not merged (found by a property test).
2. The replay settled only at the end, so capacity filled and the fill rate read
   0.75%. It now settles in time order.

## Tests
167 passing (1 skipped until live fixtures exist). Coverage 96% overall;
execution/ 97 to 100%.
