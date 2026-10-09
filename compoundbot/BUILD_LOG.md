# BUILD LOG
Newest last. Each entry: built / measured / unverified.

## Preflight + Step 0 + Step 1
Built: project skeleton, pinned-range deps, CLAUDE.md, config models (extra=forbid), risk.yaml hash check.
Measured: network preflight (all market-data hosts blocked; see docs/ASSUMPTIONS.md). Tests: config loads, bad values rejected, hash change detected.
Unverified: broker/API facts (no network). Inception dates in universe.yaml are ESTIMATED.

## Step 2 - Data layer
Built: NYSE calendar (rule based), seeded SYNTHETIC generator (vol clustering, planted-edge / null worlds),
parquet cache (atomic, incremental), providers (CSV importer, synthetic, UNVERIFIED Alpaca REST), quality checks.
Measured: tests pass (determinism, planted edge raises trend autocorrelation, quality flags gaps/jumps).
Unverified: Alpaca bars field names; real-data quality report not produced (no network).
