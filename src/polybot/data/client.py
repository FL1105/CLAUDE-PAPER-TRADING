"""Read-only HTTP clients for the public Polymarket Gamma, Data and CLOB APIs.

Assumptions:
    * All endpoints used are public and unauthenticated. This module never sends
      credentials, never signs anything, and only issues GET requests.
    * Endpoint paths and parameters are UNVERIFIED until ``polybot probe`` has run
      against the live API (see docs/ASSUMPTIONS.md).

Failure modes guarded against:
    * Getting rate-limited / banned: a token-bucket limiter throttles every
      request, and 429/5xx/transport errors are retried with exponential backoff
      plus jitter (honouring ``Retry-After``), up to a hard retry cap.
    * Hung connections: every request has a hard timeout.
    * Silent errors: every request, retry and failure is logged; non-retryable
      4xx raise :class:`ApiError` instead of returning partial data.
    * Re-downloading immutable history: an optional on-disk cache keyed by
      (url, sorted params) serves historical pulls. Live/forward calls must pass
      ``cache=False`` (the default) so they always see fresh data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from polybot.config import ApiConfig

log = logging.getLogger("polybot.client")

JSON = Any
Params = Mapping[str, str | int | float | bool | None]

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """Non-retryable HTTP error, or retries exhausted."""

    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class TokenBucket:
    """Classic token bucket. ``acquire`` blocks until a token is available.

    ``clock`` and ``sleep`` are injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        rate_per_sec: float,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0 or burst < 1:
            raise ValueError("rate_per_sec must be > 0 and burst >= 1")
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> float:
        """Take one token; returns seconds slept waiting for it."""
        waited = 0.0
        with self._lock:
            self._refill()
            while self._tokens < 1.0:
                need = (1.0 - self._tokens) / self.rate
                self._sleep(need)
                waited += need
                self._refill()
            self._tokens -= 1.0
        return waited


class ResponseCache:
    """On-disk JSON cache keyed by sha256(url + canonical params)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def key(url: str, params: Params | None) -> str:
        canon = json.dumps(
            {
                "url": url,
                "params": {k: v for k, v in sorted((params or {}).items()) if v is not None},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, url: str, params: Params | None) -> JSON | None:
        p = self._path(self.key(url, params))
        if not p.exists():
            return None
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def store(self, url: str, params: Params | None, payload: JSON) -> None:
        p = self._path(self.key(url, params))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(p)  # atomic on POSIX: no torn cache entries after a crash


class HttpClient:
    """Rate-limited, retrying, optionally caching GET-only JSON client."""

    def __init__(
        self,
        cfg: ApiConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        seed: int = 0,
        cache: ResponseCache | None = None,
    ) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._rng = random.Random(seed)  # seeded jitter: deterministic retry schedule
        self.bucket = TokenBucket(cfg.rate_limit_per_sec, cfg.rate_limit_burst, clock, sleep)
        self.cache = cache
        self._http = httpx.Client(
            timeout=httpx.Timeout(cfg.timeout_s),
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )
        self.stats = {"requests": 0, "retries": 0, "cache_hits": 0, "errors": 0}
        self.last_response: httpx.Response | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(float(retry_after), self.cfg.backoff_max_s)
            except ValueError:
                pass  # HTTP-date form; fall through to exponential backoff
        base = min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * (2.0**attempt))
        return base * (0.5 + self._rng.random() / 2)  # "equal jitter"

    def get_json(self, url: str, params: Params | None = None, *, cache: bool = False) -> JSON:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if cache and self.cache is not None:
            hit = self.cache.get(url, clean)
            if hit is not None:
                self.stats["cache_hits"] += 1
                log.debug("cache hit %s %s", url, clean)
                return hit

        attempt = 0
        while True:
            self.bucket.acquire()
            self.stats["requests"] += 1
            t0 = time.monotonic()
            try:
                resp = self._http.get(url, params=clean)
            except httpx.TransportError as exc:
                if attempt >= self.cfg.max_retries:
                    self.stats["errors"] += 1
                    raise ApiError(
                        f"transport error after {attempt} retries: {exc}", url=url
                    ) from exc
                delay = self._backoff(attempt, None)
                log.warning(
                    "transport error %s on %s; retry %d in %.2fs", exc, url, attempt + 1, delay
                )
                self.stats["retries"] += 1
                attempt += 1
                self._sleep(delay)
                continue

            self.last_response = resp
            elapsed_ms = (time.monotonic() - t0) * 1000
            log.info("GET %s params=%s status=%d ms=%.0f", url, clean, resp.status_code, elapsed_ms)
            if resp.status_code in RETRYABLE_STATUS:
                if attempt >= self.cfg.max_retries:
                    self.stats["errors"] += 1
                    raise ApiError(
                        f"HTTP {resp.status_code} after {attempt} retries", resp.status_code, url
                    )
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                log.warning(
                    "HTTP %d on %s; retry %d in %.2fs", resp.status_code, url, attempt + 1, delay
                )
                self.stats["retries"] += 1
                attempt += 1
                self._sleep(delay)
                continue
            if resp.status_code >= 400:
                self.stats["errors"] += 1
                raise ApiError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code, url)
            try:
                payload = resp.json()
            except ValueError as exc:
                self.stats["errors"] += 1
                raise ApiError(f"non-JSON body from {url}", resp.status_code, url) from exc
            if cache and self.cache is not None:
                self.cache.store(url, clean, payload)
            return payload


class PolymarketClient:
    """Thin endpoint wrappers. Returns RAW JSON; parsing happens in ``data.schemas``.

    Keeping raw-fetch and parsing separate is the adapter layer: if the probe
    shows a schema differs from what we assumed, only ``schemas.py`` changes.
    """

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.gamma = http.cfg.gamma_base_url.rstrip("/")
        self.data = http.cfg.data_base_url.rstrip("/")
        self.clob = http.cfg.clob_base_url.rstrip("/")

    # --- Gamma -----------------------------------------------------------------
    def markets(self, *, cache: bool = False, **params: Any) -> JSON:
        return self.http.get_json(f"{self.gamma}/markets", params, cache=cache)

    def events(self, *, cache: bool = False, **params: Any) -> JSON:
        return self.http.get_json(f"{self.gamma}/events", params, cache=cache)

    # --- Data API --------------------------------------------------------------
    def trades(self, *, cache: bool = False, **params: Any) -> JSON:
        return self.http.get_json(f"{self.data}/trades", params, cache=cache)

    def positions(self, user: str, **params: Any) -> JSON:
        return self.http.get_json(f"{self.data}/positions", {"user": user, **params})

    def activity(self, user: str, *, cache: bool = False, **params: Any) -> JSON:
        return self.http.get_json(f"{self.data}/activity", {"user": user, **params}, cache=cache)

    def value(self, user: str) -> JSON:
        return self.http.get_json(f"{self.data}/value", {"user": user})

    # --- CLOB (public read-only endpoints only) ---------------------------------
    def book(self, token_id: str) -> JSON:
        return self.http.get_json(f"{self.clob}/book", {"token_id": token_id})

    def price(self, token_id: str, side: str = "BUY") -> JSON:
        return self.http.get_json(f"{self.clob}/price", {"token_id": token_id, "side": side})

    def midpoint(self, token_id: str) -> JSON:
        return self.http.get_json(f"{self.clob}/midpoint", {"token_id": token_id})

    def prices_history(self, token_id: str, *, cache: bool = False, **params: Any) -> JSON:
        return self.http.get_json(
            f"{self.clob}/prices-history", {"market": token_id, **params}, cache=cache
        )
