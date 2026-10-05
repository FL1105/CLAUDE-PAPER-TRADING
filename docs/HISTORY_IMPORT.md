# Importing historical trades

The public `/trades` feed probably cannot page back far enough for 4 to 6 months
of history (see ASSUMPTIONS). Bulk on-chain datasets can. **None of the dataset
layouts below has been verified.** The build sandbox could not reach Hugging
Face or GitHub. So always inspect first.

## Workflow

```bash
pip install -e ".[history]"            # DuckDB + pyarrow

# 1. Look at the columns (works on local files, globs, https:// and hf:// paths)
polybot history inspect PATH

# 2. If columns already match the API names (proxyWallet, side, asset, conditionId,
#    size, price, timestamp, outcomeIndex, transactionHash), import directly:
polybot history import PATH --name mysource --start 2026-04-01 --end 2026-10-01

# 3. Otherwise write a mapping SQL file with a {src} placeholder and pass --mapping.

# 4. Attach markets + resolutions (needs API access):
polybot backfill --markets-only
```

Only the date window is read, because DuckDB pushes the filter into Parquet scans.
Every row goes through the same validator as live API data; bad rows are counted in
`schema_drift`, never silently fixed. Re-importing is safe (idempotent).
`conditionId` / `outcomeIndex` may be missing; they are linked from token ids.

## Candidate sources (UNVERIFIED leads, found by web search 2026-10-05)

| source | what it reportedly is | notes |
|---|---|---|
| `huggingface.co/datasets/SII-WANGZJ/Polymarket_data` | ~1.1 to 1.9B on-chain records, `trades.parquet` + market metadata | 100+ GB in total; query a window via `hf://datasets/SII-WANGZJ/Polymarket_data/...` |
| Envio Polymarket dataset on Hugging Face | every CLOB fill 2020 to Apr 2026, about 1.17B fills | DuckDB over HTTPS; see docs.envio.dev/blog/polymarket-onchain-data |
| `github.com/warproxxx/poly_data` | pipeline: CTF Exchange **V2** contract -> trades CSV | only route found for data **after the reported 2026-04-28 contract migration** |

Reported caveat: Polymarket moved to new exchange contracts on 2026-04-28. Older
datasets may stop there, and old subgraph pipelines may be incomplete after it.

## Mapping template for raw on-chain fills (UNVERIFIED; adjust after inspect)

On-chain `OrderFilled` events have a maker and a taker, asset ids (0 = USDC) and
filled amounts (6 decimals). One way to derive the **maker's** trade:

```sql
SELECT
  maker                                         AS proxyWallet,
  CASE WHEN makerAssetId = '0' THEN 'BUY' ELSE 'SELL' END AS side,
  CASE WHEN makerAssetId = '0' THEN takerAssetId ELSE makerAssetId END AS asset,
  NULL                                          AS conditionId,
  CASE WHEN makerAssetId = '0' THEN takerAmountFilled ELSE makerAmountFilled END / 1e6 AS size,
  CASE WHEN makerAssetId = '0' THEN makerAmountFilled / takerAmountFilled
       ELSE takerAmountFilled / makerAmountFilled END AS price,
  "timestamp"                                   AS "timestamp",
  NULL                                          AS outcomeIndex,
  transactionHash                               AS transactionHash
FROM {src}
```

Caution: when one taker order matches several makers, the taker side may appear
as the exchange contract or be split across events. Counting both sides naively
can double count. Check a few transactions by hand against the Data API
`/trades?user=` before trusting a mapping, and record what you found in
ASSUMPTIONS.md.
