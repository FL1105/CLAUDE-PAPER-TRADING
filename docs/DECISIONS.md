# DECISIONS

Each judgment call, why, and what would change it. New decisions are appended.

## D1. Unit of observation = (wallet, market), not trade
Multiple trades by one wallet in one market are strongly correlated (same thesis,
same outcome). Treating them as independent inflates n and the z-score.
Aggregating to one observation per (wallet, market) with a size-weighted entry
price is conservative.

## D2. Benjamini-Hochberg over Bonferroni
We test thousands of wallets. Bonferroni controls the chance of *any* false
discovery and would leave almost no power. BH controls the expected *fraction* of
false discoveries among selected wallets (q = 0.10), which is the quantity that
matters for a portfolio of copied wallets. False discoveries that slip through
are then caught by the out-of-sample gates and the placebo comparison.

## D3. Hold to resolution in v1
We do not mirror wallet exits. Exit timing adds a second latency-sensitive leg
and a second fill-quality problem, and hold-to-resolution edge is the only edge
our per-wallet test measures. Wallet sells are logged as informational.

## D4. No Kelly, no martingale, fixed fractional stake
Edge estimates are uncertain. Full Kelly on an overestimated edge overbets and
can ruin the bankroll. A fixed 0.5% stake with per-market and total exposure
caps keeps paper results interpretable.

## D5. Separate raw fetching from parsing (adapter layer)
`data/client.py` returns raw JSON; `data/schemas.py` is the only place that
knows field names. Schema drift then fails loudly in one place
(`SchemaDriftError`, counted per endpoint), and a schema fix touches one module.

## D6. Resolution only from settlement prices + closed flag
Last trade price at 0.99 is not resolution. Markets can be voided, disputed or
settled 50-50. Unclean closed states are `AMBIGUOUS` and excluded.

## D7. Config is frozen, hashed and rejects unknown keys
A typo'd threshold must not silently fall back to a default. The hash is stamped
on every paper fill; gate 12 fails if it changed during the forward test.

## D8. Deterministic retry jitter
The HTTP client's jitter RNG is seeded so test runs and retry schedules are
reproducible. Jitter still spreads load because real network timing differs.

## D9. Safety by absence, enforced by a test
There is no signing code, no key handling and no authenticated client.
`tests/test_safety.py` scans `src/` and `scripts/` for order or key related
patterns, including any HTTP verb other than GET. Adding a real-money path
requires deleting that test, which a reviewer will see.
