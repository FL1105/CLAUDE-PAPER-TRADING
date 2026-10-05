# ASSUMPTIONS

Status legend: **UNVERIFIED** = believed from documentation / prior knowledge, not
observed. **VERIFIED** = observed by `polybot probe` (auto-generated section at the
bottom of this file, which replaces the probe block on every run).

## 0. Build-environment limitation (read this first)

Phase 1 was built in a cloud sandbox whose network policy **denied every
`*.polymarket.com` host** (`gamma-api`, `data-api`, `clob`, and `docs`), with
HTTP 403 at the egress proxy. Therefore:

- **No schema in this repo has been verified against the live API yet.**
- `tests/fixtures/assumed/` holds hand-written fixtures with fake IDs. They are
  labeled as assumed and are used only to unit-test the adapter layer.
- The fee schedule was **not** checked against the official docs.
- `polybot probe` is fully built and tested against a fake API; it must be run
  on a machine with network access before Phase 3 (ingestion) begins.

## 1. Assumed endpoints (UNVERIFIED)

| API | Path | Params we use | Assumed response |
|---|---|---|---|
| Gamma | `/markets` | `limit, offset, closed, active, order, ascending` | list of market objects |
| Gamma | `/events` | same | list of event objects (not yet parsed) |
| Data | `/trades` | `limit, offset, user` (`takerOnly`, `market`, `side` may exist) | list of trades |
| Data | `/positions` | `user, limit` | list of positions |
| Data | `/activity` | `user, limit, offset, start, end` | list of activity rows |
| Data | `/value` | `user` | `[{"user","value"}]` |
| CLOB | `/book` | `token_id` | book object |
| CLOB | `/price` | `token_id, side` | `{"price": "0.62"}` |
| CLOB | `/midpoint` | `token_id` | `{"mid": "0.615"}` |
| CLOB | `/prices-history` | `market=<token_id>, interval, fidelity` | `{"history":[{"t","p"}]}` |
| CLOB | `/fee-rate` | `token_id` | **existence unknown**: the probe tries it and records the result |

## 2. Assumed schemas (UNVERIFIED; encoded in `src/polybot/data/schemas.py`)

**Gamma market**: `conditionId` (str), `question`, `endDate` (ISO-8601),
`closedTime` (ISO-like, e.g. `"2026-01-01 03:12:45+00"`), `closed` (bool),
`outcomes` / `outcomePrices` / `clobTokenIds` as **JSON-encoded strings** (lists
also accepted), `negRisk` (bool), `category` (optional, often missing).

**Data trade**: `proxyWallet`, `side` (`BUY`/`SELL`), `asset` (token id),
`conditionId`, `size` (shares), `price` (0..1), `timestamp` (epoch seconds),
`outcomeIndex`, `transactionHash`.

**CLOB book**: `asset_id`, `timestamp` (epoch **ms** as string), `bids`/`asks` as
`[{"price": str, "size": str}]`, `tick_size`, `min_order_size`. We assume the
level order may be anything (historically asks were returned *descending*), so the
adapter sorts explicitly: asks ascending, bids descending.

## 3. Semantics we rely on (UNVERIFIED)

- **Resolution**: a market is `RESOLVED` only if `closed == true` and the final
  `outcomePrices` are exactly one-hot (`1`/`0`). All-equal prices summing to 1
  (e.g. `0.5/0.5`) = `VOID` (50-50 / invalid): excluded from edge statistics, and
  paper positions settle at the refund value. Any other closed state is
  `AMBIGUOUS`: excluded and counted, never guessed. **We never infer a winner
  from a last trade price.** Whether `umaResolutionStatus` should additionally be
  required is an open question for the probe.
- **Trade uniqueness**: `transactionHash` alone is not unique (one transaction can
  fill several orders), so `trade_key = sha256(tx, wallet, token, side, size,
  price, ts)`.
- **Trade feed default**: the Data API `/trades` may default to taker-only
  trades. If so, maker fills by a wallet are invisible to us; the probe compares
  counts with `takerOnly=false`.
- **Deep history**: `/trades` likely caps `limit` and `offset` (a few thousand
  rows). If so, a 90-day global backfill is **not** achievable from `/trades`
  alone and per-wallet `/activity` (with `start`/`end`) or per-market queries
  are needed. The real achievable window will be recorded here by the probe.

## 4. Fees (UNVERIFIED, configurable)

Default model (spec section 5.4, `config/default.yaml: fees`):

    fee = fee_coeff * shares * p * (1 - p)        fee_coeff default 0.05

Polymarket reportedly introduced price-dependent taker fees on some market
categories in 2026. Neither the formula, the coefficient, nor which categories are
fee-bearing was checked (docs host blocked). To do on a networked machine:

1. Read the official fee documentation and record the formula, coefficients and
   categories here, with the date read.
2. Check whether `/fee-rate` or any Gamma market field (the probe lists all keys
   containing "fee") exposes per-market fee data.
3. Update `fees` in the config (with a `config_version` bump and a GATES.md
   changelog entry) if anything differs.

All gates are evaluated at 1x and 2x the fee coefficient regardless.

## 5. Rate limits (UNVERIFIED)

Default client limit is a conservative 2 req/s, burst 4, with backoff on
429/5xx. The probe records any rate-limit headers it sees.

## 6. Ingestion assumptions (Phase 3, UNVERIFIED)

- `/trades` and `/activity` are newest-first and paginate with `limit`/`offset`.
  `ingest.page_limit` (500) and `ingest.max_offset` (3000) are guesses; set them
  from the probe's pagination table. When the cap is hit, a `history_cap_reached`
  anomaly records the oldest timestamp actually reached.
- `/activity?user=` accepts `start`/`end` and returns `type: TRADE` rows with the
  `/trades` fields. This is the per-wallet route to deeper history.
- Gamma `/markets` accepts repeated `condition_ids=` and `clob_token_ids=` for
  lookups. Markets not returned are logged as `market_not_found`.
- Offset pagination over a moving feed can shift between pages. Overlap is
  harmless (idempotent inserts). A forward poll that hits the cap without
  catching up logs `forward_gap_possible`.
- Bulk history: see docs/HISTORY_IMPORT.md. CSVs are read as text so IDs like
  `0xc` are never re-typed as numbers. That bug was found and fixed in testing.

## 7. Execution assumptions (Phase 4)

- **Synthetic order book (backtests only).** No historical order books exist, so
  backtest fills walk a synthetic ladder: best ask = reference + `sim_half_spread`
  (1 cent), `sim_levels` (5) levels one tick apart, `sim_level_depth_usdc` ($250)
  each. These numbers are **guesses**. Replace them with spreads and depth
  measured from the forward run's real book snapshots before trusting backtest
  costs. Live-forward fills always use the real book.
- **Reference price in real-data backtests** = last real trade in the same
  token in [t, t + delay]: the tape. It is a traded price, not a quote, which is
  why the synthetic spread is added on top.
- Shares are rounded down to 2 decimals and prices snapped up to the tick
  (pessimistic for a buyer). The real minimum order size is not enforced yet;
  `min_order_size` from `/book` is stored for when it is verified.
- Fees are charged per fill level with the parabolic formula (section 4).

## 8. Deviations from the build prompt

- Project lives at the repository root rather than in a `polybot/` subfolder.
- Phase 1 also includes `data/client.py`, `data/schemas.py` (adapter layer) and
  the `execution/live.py` stub, because the probe needs a client and the
  no-real-money constraint should hold from the first commit.
- Commands for later phases exist in the CLI but exit with code 2 and the phase
  number that will implement them.
- Phase 3 adds `polybot walkforward` (gross OOS at the wallet's own prices) and
  `polybot history inspect|import`. `backtest` (with placebo, stress, costs)
  arrives in Phase 5.
