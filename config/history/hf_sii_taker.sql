-- Maps SII-WANGZJ/Polymarket_data trades.parquet to the API trade shape using the TAKER side
-- (the public /trades feed the live loop watches is taker-side), restricted to the wallets in
-- out/hf/wallets.parquet (made by scripts/hf_shortlist.py). Taker direction is for asset_id.
SELECT
  taker                AS proxyWallet,
  taker_direction      AS side,
  asset_id             AS asset,
  condition_id         AS conditionId,
  token_amount         AS size,
  price                AS price,
  "timestamp"          AS "timestamp",
  NULL                 AS outcomeIndex,
  transaction_hash     AS transactionHash
FROM {src}
WHERE taker IN (SELECT wallet FROM read_parquet('out/hf/wallets.parquet'))
