from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from polybot.config import Config
from polybot.data.client import ApiError, HttpClient, PolymarketClient, ResponseCache, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make(cfg: Config, handler, clock: FakeClock | None = None, cache: ResponseCache | None = None):
    clock = clock or FakeClock()
    http = HttpClient(
        cfg.api,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
        seed=1,
        cache=cache,
    )
    return http, clock


def test_token_bucket_throttles() -> None:
    clk = FakeClock()
    b = TokenBucket(2.0, 2, clock=clk, sleep=clk.sleep)
    for _ in range(6):
        b.acquire()
    # 2 burst tokens free, then 4 more at 2/s => 2 seconds of waiting
    assert sum(clk.slept) == pytest.approx(2.0)


def test_token_bucket_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 1)


def test_retries_on_429_then_succeeds(cfg: Config) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, json={"ok": True})

    http, clk = make(cfg, handler)
    assert http.get_json("https://x/y") == {"ok": True}
    assert calls["n"] == 3
    assert http.stats["retries"] == 2
    assert 1.5 in clk.slept  # Retry-After honoured


def test_exponential_backoff_with_jitter_on_5xx(cfg: Config) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    http, clk = make(cfg, handler)
    with pytest.raises(ApiError) as ei:
        http.get_json("https://x/y")
    assert ei.value.status == 503
    backoffs = [s for s in clk.slept if s > 0.0]
    # max_retries backoff sleeps, each within [0.5, 1.0] * base * 2^k (capped)
    retry_sleeps = backoffs[-cfg.api.max_retries :]
    for k, s in enumerate(retry_sleeps):
        base = min(cfg.api.backoff_max_s, cfg.api.backoff_base_s * 2**k)
        assert 0.5 * base <= s <= base


def test_backoff_is_deterministic_given_seed(cfg: Config) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    runs = []
    for _ in range(2):
        http, clk = make(cfg, handler)
        with pytest.raises(ApiError):
            http.get_json("https://x/y")
        runs.append(clk.slept)
    assert runs[0] == runs[1]


def test_transport_error_retried(cfg: Config) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json=[1])

    http, _ = make(cfg, handler)
    assert http.get_json("https://x/y") == [1]


def test_4xx_not_retried(cfg: Config) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad offset")

    http, _ = make(cfg, handler)
    with pytest.raises(ApiError) as ei:
        http.get_json("https://x/y")
    assert ei.value.status == 400 and calls["n"] == 1


def test_non_json_raises(cfg: Config) -> None:
    http, _ = make(cfg, lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ApiError):
        http.get_json("https://x/y")


def test_cache_hit_avoids_network(cfg: Config, tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"v": calls["n"]})

    http, _ = make(cfg, handler, cache=ResponseCache(tmp_path))
    a = http.get_json("https://x/y", {"b": 2, "a": 1}, cache=True)
    b = http.get_json("https://x/y", {"a": 1, "b": 2}, cache=True)  # param order irrelevant
    c = http.get_json("https://x/y", {"a": 1, "b": 2})  # cache=False -> fresh
    assert a == b == {"v": 1} and c == {"v": 2}
    assert http.stats["cache_hits"] == 1


def test_none_params_dropped_and_get_only(cfg: Config) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[])

    http, _ = make(cfg, handler)
    pm = PolymarketClient(http)
    pm.trades(limit=5, user=None)
    pm.book("123")
    assert all(r.method == "GET" for r in seen)
    assert "user" not in seen[0].url.params and seen[0].url.params["limit"] == "5"
    assert seen[1].url.path == "/book" and seen[1].url.params["token_id"] == "123"
    assert "authorization" not in {k.lower() for k in seen[0].headers}
