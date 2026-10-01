"""The single path every Zoho API call takes.

    cache / single-flight -> daily budget -> minute bucket -> concurrency gate
      -> auth header + organization_id -> HTTP -> classify -> retry or raise
"""

import asyncio
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from zoho_inventory_mcp.auth.oauth import TokenProvider
from zoho_inventory_mcp.client.cache import ResponseCache
from zoho_inventory_mcp.client.errors import (
    AuthExpired,
    ConcurrencyLimited,
    DailyLimitExceeded,
    PermissionDenied,
    RateLimitedMinute,
    UpstreamUnavailable,
    ZohoError,
    classify,
)
from zoho_inventory_mcp.client.ratelimit import PLAN_CONCURRENCY, DailyBudget, MinuteBucket

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class ClientStats:
    upstream_requests: int = 0
    retries: int = 0
    errors_by_type: Counter = field(default_factory=Counter)  # error kind -> count (incl. rate limits)
    fast_failed: int = 0  # calls refused locally rather than waiting too long
    in_flight: int = 0
    peak_in_flight: int = 0


@dataclass(frozen=True)
class Fetched:
    data: dict[str, Any]
    cached: bool


class ZohoClient:
    # Longest we'll stall one tool call waiting out a limit; past this we tell the agent instead.
    MAX_WAIT_SECONDS = 20.0
    MINUTE_LIMIT_RETRIES = 2
    CONCURRENCY_RETRIES = 3
    UPSTREAM_RETRIES = 3

    def __init__(
        self,
        tokens: TokenProvider,
        http: httpx.AsyncClient,
        *,
        plan: str = "free",
        per_minute: int = 90,
        max_concurrency: int | None = None,
        bucket: MinuteBucket | None = None,
        daily: DailyBudget | None = None,
        cache: ResponseCache | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        self._tokens = tokens
        self._http = http
        self.bucket = bucket or MinuteBucket(per_minute)
        self.daily = daily or DailyBudget(plan)
        self.cache = cache or ResponseCache()
        self._gate = asyncio.Semaphore(max_concurrency or PLAN_CONCURRENCY.get(plan, 5))
        self._sleep = sleep
        self._jitter = jitter
        self.stats = ClientStats()

    @property
    def organization_id(self) -> str:
        return self._tokens.tokens.organization_id or ""

    @property
    def organization_name(self) -> str:
        return self._tokens.tokens.organization_name or ""

    @property
    def region(self) -> str:
        return self._tokens.tokens.region.code

    async def get(
        self, path: str, params: dict[str, Any] | None = None, *, cache_ttl: float = 0.0, fresh: bool = False
    ) -> Fetched:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        key = f"{path}?{urlencode(sorted(clean.items()))}"
        data, cached = await self.cache.get_or_fetch(
            key, cache_ttl, lambda: self._send("GET", path, clean), fresh=fresh
        )
        return Fetched(data, cached)

    async def post(self, path: str, json: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Writes are used only by the demo seed script; the MCP tools never call this."""
        return await self._send("POST", path, params or {}, json=json)

    @asynccontextmanager
    async def _slot(self):
        async with self._gate:
            self.stats.in_flight += 1
            self.stats.peak_in_flight = max(self.stats.peak_in_flight, self.stats.in_flight)
            try:
                yield
            finally:
                self.stats.in_flight -= 1

    async def _pace(self) -> None:
        granted, delay = self.bucket.reserve(max_wait=self.MAX_WAIT_SECONDS)
        if not granted:
            self.stats.fast_failed += 1
            raise RateLimitedMinute(
                "Connector is pacing requests to stay under Zoho's per-minute limit.", retry_after=delay
            )
        if delay > 0:
            await self._sleep(delay)

    async def _send(
        self, method: str, path: str, params: dict[str, Any], json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self._tokens.tokens.region.inventory_base_url}/{path.lstrip('/')}"
        query = {"organization_id": self.organization_id, **params}
        # A 429 means Zoho did not process the call, so any method may retry it; other
        # failures are retried only for reads, which are safe to repeat.
        idempotent = method == "GET"
        attempts: Counter = Counter()
        refreshed = False

        while True:
            self.daily.check()
            await self._pace()
            token = await self._tokens.access_token()
            try:
                async with self._slot():
                    self.stats.upstream_requests += 1
                    response = await self._http.request(
                        method,
                        url,
                        params=query,
                        json=json,
                        headers={"Authorization": f"Zoho-oauthtoken {token}"},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                error: ZohoError = UpstreamUnavailable(f"Network error: {type(exc).__name__}")
            else:
                self.daily.record(response.headers)
                maybe_error = classify(response)
                if maybe_error is None:
                    return response.json()
                error = maybe_error

            self.stats.errors_by_type[error.kind] += 1
            attempts[error.kind] += 1
            n = attempts[error.kind]

            if isinstance(error, AuthExpired):
                if refreshed:
                    raise PermissionDenied(error.message, zoho_code=error.zoho_code, http_status=401)
                refreshed = True
                await self._tokens.handle_rejected(token)
                continue

            if isinstance(error, DailyLimitExceeded):
                self.daily.mark_exhausted(error.retry_after)
                raise error

            if isinstance(error, RateLimitedMinute):
                wait = error.retry_after or 60.0
                self.bucket.penalize(wait)
                if n > self.MINUTE_LIMIT_RETRIES or wait > self.MAX_WAIT_SECONDS:
                    error.retry_after = wait
                    raise error
                self.stats.retries += 1
                continue  # _pace() sleeps until the penalty has passed

            if isinstance(error, ConcurrencyLimited) and n <= self.CONCURRENCY_RETRIES:
                self.stats.retries += 1
                await self._sleep(self._jitter(0.5, 1.5) * 2 ** (n - 1))
                continue

            if isinstance(error, UpstreamUnavailable) and idempotent and n <= self.UPSTREAM_RETRIES:
                self.stats.retries += 1
                await self._sleep(2 ** (n - 1) + self._jitter(0, 0.5))
                continue

            raise error

    async def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        *,
        per_page: int = 200,
        max_pages: int = 3,
        cache_ttl: float = 0.0,
        fresh: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Collect up to `max_pages` pages. Returns (records, truncated).

        The page cap stops one tool call from quietly burning dozens of API calls.
        """
        records: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            fetched = await self.get(
                path, {**(params or {}), "page": page, "per_page": per_page}, cache_ttl=cache_ttl, fresh=fresh
            )
            records.extend(fetched.data.get(key, []))
            if not fetched.data.get("page_context", {}).get("has_more_page"):
                return records, False
        return records, True

    def status(self) -> dict[str, Any]:
        return {
            "organization": {"id": self.organization_id, "name": self.organization_name},
            "region": self.region,
            "scopes": self._tokens.tokens.scopes,
            "daily_quota": self.daily.snapshot(),
            "session": {
                "upstream_requests": self.stats.upstream_requests,
                "cache_hits": self.cache.hits,
                "retries": self.stats.retries,
                "errors_by_type": dict(self.stats.errors_by_type),
                "fast_failed": self.stats.fast_failed,
                "peak_concurrency": self.stats.peak_in_flight,
            },
        }
