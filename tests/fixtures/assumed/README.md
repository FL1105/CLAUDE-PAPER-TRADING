# ASSUMED fixtures — NOT real API responses

These files were hand-written because the build environment could not reach
Polymarket (network policy blocked `*.polymarket.com`). They encode the schemas
we *assume* from public documentation as remembered at build time.

They exist only so the adapter layer (`src/polybot/data/schemas.py`) and probe
can be unit-tested. All IDs are obviously fake (`0xa5a5…`, `0x1111…`).

Once `polybot probe` runs on a machine with network access, real responses land
in `tests/fixtures/live/`, the test suite automatically validates the adapters
against them too, and any drift is reported in `docs/ASSUMPTIONS.md`.
