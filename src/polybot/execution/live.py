"""LiveExecutor STUB. v1 contains no real-money code path, by design.

Assumption: real-money trading is out of scope until a candidate has passed every
paper gate (S2) AND the human has completed the manual real-money checklist. Even
then, live trading is a separate future task with a separate review.

Failure mode guarded against: accidental live trading. There is no signing code,
no key handling and no authenticated client anywhere in this package; this class
exists only so the interface is visible, and constructing it raises.
"""

from __future__ import annotations

from typing import NoReturn

LIVE_TRADING_ENABLED = False


class LiveExecutor:
    """Placeholder. Every entry point raises NotImplementedError."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "LiveExecutor is a stub. polybot v1 has no real-money code paths "
            f"(LIVE_TRADING_ENABLED={LIVE_TRADING_ENABLED})."
        )

    def submit(self, *args: object, **kwargs: object) -> NoReturn:
        raise NotImplementedError("LiveExecutor is a stub; no orders can be placed.")
