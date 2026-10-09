# SOURCES (verify before relying; record URL and date)

| Item | Where to verify | Status |
|---|---|---|
| Alpaca Trading API v2 (account, positions, orders, by_client_order_id, paper base URL) | Alpaca docs (docs.alpaca.markets) | UNVERIFIED, written from memory without network |
| Alpaca Market Data v2 stock bars / latest trade; crypto latest trades | Alpaca docs | UNVERIFIED |
| Alpaca crypto fee tiers, fractional share support, paper-account behaviour | Alpaca docs / account page | UNVERIFIED (defaults in config/costs.yaml are ESTIMATED) |
| ETF inception dates in config/universe.yaml | Issuer fact sheets | ESTIMATED |
| NYSE holiday rules (calendar.py) | NYSE holiday calendar | Implemented from rules; spot-checked against known holidays in tests |
| Pattern-day-trader and account-type rules | Broker and FINRA documentation | UNVERIFIED; core strategies use daily bars only |
| Sharpe estimation formulas (PSR, DSR, MinTRL) | Bailey and Lopez de Prado, "The Sharpe Ratio Efficient Frontier" and "The Deflated Sharpe Ratio" | Implemented from the papers' formulas; unit-tested for properties |
