import asyncio

import httpx
import pytest
import respx

from zoho_inventory_mcp.client.cache import ResponseCache
from zoho_inventory_mcp.client.errors import (
    BadRequest,
    DailyLimitExceeded,
    NotFound,
    PermissionDenied,
    RateLimitedMinute,
    UpstreamUnavailable,
    classify,
)
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.client.ratelimit import DailyBudget, MinuteBucket

from helpers import BASE, ORG, FakeTime, FakeTokens, ok, zoho_error

@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


@pytest.fixture
async def client(ft):
    async with httpx.AsyncClient() as http:
        yield ZohoClient(
            FakeTokens(),
            http,
            bucket=MinuteBucket(60, clock=ft.clock),
            daily=DailyBudget("free", clock=ft.clock),
            cache=ResponseCache(clock=ft.clock),
            sleep=ft.sleep,
            jitter=lambda low, high: low,
        )


# --- MinuteBucket -------------------------------------------------------------------


def test_bucket_grants_burst_then_spaces_requests(ft):
    bucket = MinuteBucket(60, clock=ft.clock)
    delays = [bucket.reserve()[1] for _ in range(62)]
    assert delays[:60] == [0.0] * 60
    assert delays[60] == pytest.approx(1.0)
    assert delays[61] == pytest.approx(2.0)


def test_bucket_refills_over_time(ft):
    bucket = MinuteBucket(60, clock=ft.clock)
    for _ in range(60):
        bucket.reserve()
    ft.now += 30
    assert [bucket.reserve()[1] for _ in range(30)] == [0.0] * 30
    assert bucket.reserve()[1] > 0


def test_bucket_refuses_without_booking_when_wait_too_long(ft):
    bucket = MinuteBucket(60, clock=ft.clock)
    for _ in range(80):
        bucket.reserve()
    granted, delay = bucket.reserve(max_wait=20)
    assert not granted and delay == pytest.approx(21.0)
    # The refused call did not consume a slot.
    assert bucket.reserve()[1] == pytest.approx(21.0)


def test_penalty_blocks_until_it_expires(ft):
    bucket = MinuteBucket(60, clock=ft.clock)
    bucket.penalize(30)
    assert bucket.reserve()[1] == pytest.approx(30.0)
    assert bucket.reserve()[1] == pytest.approx(31.0)


# --- DailyBudget --------------------------------------------------------------------


def test_daily_budget_prefers_zoho_headers(ft):
    budget = DailyBudget("free", clock=ft.clock)
    budget.record({"x-rate-limit-limit": "7500", "x-rate-limit-remaining": "700", "x-rate-limit-reset": "3600"})
    assert budget.snapshot() == {"limit": 7500, "remaining": 700, "resets_in_seconds": 3600}
    assert budget.low


def test_daily_budget_refuses_locally_until_reset(ft):
    budget = DailyBudget("free", clock=ft.clock)
    budget.mark_exhausted(retry_after=100)
    with pytest.raises(DailyLimitExceeded) as exc:
        budget.check()
    assert exc.value.retry_after == pytest.approx(100)
    ft.now += 101
    budget.check()  # past reset: allowed to try again


# --- classify -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (zoho_error(429, 44), RateLimitedMinute),
        (zoho_error(429, 45), DailyLimitExceeded),
        (zoho_error(429, 44, **{"x-rate-limit-remaining": "0"}), DailyLimitExceeded),
        (zoho_error(404, 1002), NotFound),
        (zoho_error(400, 2), BadRequest),
        (zoho_error(200, 9), BadRequest),  # Zoho sometimes reports failures with HTTP 200
        (zoho_error(503, 0), UpstreamUnavailable),
    ],
)
def test_classify(response, expected):
    assert isinstance(classify(response), expected)


def test_classify_success_is_none():
    assert classify(ok()) is None


def test_agent_payload_shape():
    payload = DailyLimitExceeded("limit", retry_after=3599.6).to_agent()["error"]
    assert payload["type"] == "daily_limit_exceeded"
    assert payload["retryable"] is False
    assert payload["retry_after_seconds"] == 3600
    assert "do not keep retrying" in payload["message"]


# --- ZohoClient: request basics --------------------------------------------------------


@respx.mock
async def test_get_injects_org_and_auth_header(client):
    route = respx.get(f"{BASE}/items").mock(return_value=ok({"items": [{"item_id": "1"}]}))
    fetched = await client.get("items", {"search_text": "kurta", "sku": None})
    request = route.calls.last.request
    assert request.url.params["organization_id"] == ORG
    assert request.url.params["search_text"] == "kurta"
    assert "sku" not in request.url.params  # None params are dropped
    assert request.headers["Authorization"] == "Zoho-oauthtoken t1"
    assert fetched.data["items"] == [{"item_id": "1"}] and not fetched.cached


@respx.mock
async def test_not_found_is_not_retried(client):
    route = respx.get(f"{BASE}/items/9").mock(return_value=zoho_error(404, 1002))
    with pytest.raises(NotFound):
        await client.get("items/9")
    assert route.call_count == 1


# --- rate limits -----------------------------------------------------------------------


@respx.mock
async def test_minute_limit_waits_out_penalty_then_retries(client, ft):
    route = respx.get(f"{BASE}/items").mock(
        side_effect=[zoho_error(429, 44, **{"retry-after": "5"}), ok({"items": []})]
    )
    await client.get("items")
    assert route.call_count == 2
    assert ft.sleeps == [pytest.approx(5.0)]
    assert client.stats.retries == 1
    assert client.stats.errors_by_type["rate_limited"] == 1


@respx.mock
async def test_minute_limit_with_long_wait_is_returned_to_agent(client):
    route = respx.get(f"{BASE}/items").mock(return_value=zoho_error(429, 44, **{"retry-after": "120"}))
    with pytest.raises(RateLimitedMinute) as exc:
        await client.get("items")
    assert exc.value.retry_after == 120
    assert route.call_count == 1


@respx.mock
async def test_daily_limit_never_retries_and_blocks_later_calls_locally(client):
    route = respx.get(f"{BASE}/items").mock(
        return_value=zoho_error(429, 45, **{"x-rate-limit-reset": "7200", "x-rate-limit-remaining": "0"})
    )
    with pytest.raises(DailyLimitExceeded) as exc:
        await client.get("items")
    assert exc.value.retry_after == 7200
    with pytest.raises(DailyLimitExceeded):
        await client.get("items", {"page": 2})
    assert route.call_count == 1  # second call refused without touching Zoho


@respx.mock
async def test_concurrency_limit_backs_off_exponentially(client, ft):
    route = respx.get(f"{BASE}/items").mock(
        side_effect=[zoho_error(429, 1070), zoho_error(429, 1070), ok({"items": []})]
    )
    await client.get("items")
    assert route.call_count == 3
    assert ft.sleeps == [0.5, 1.0]  # jitter pinned to the lower bound


@respx.mock
async def test_bucket_exhaustion_fails_fast_without_calling_zoho(client):
    route = respx.get(f"{BASE}/items").mock(return_value=ok())
    for _ in range(81):
        client.bucket.reserve()
    with pytest.raises(RateLimitedMinute) as exc:
        await client.get("items")
    assert exc.value.retry_after > client.MAX_WAIT_SECONDS
    assert route.call_count == 0
    assert client.stats.fast_failed == 1


async def test_concurrency_gate_caps_in_flight_requests():
    in_flight = peak = 0

    async def slow(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return ok({"items": []})

    with respx.mock:
        respx.get(f"{BASE}/items").mock(side_effect=slow)
        async with httpx.AsyncClient() as http:
            client = ZohoClient(FakeTokens(), http, plan="free", per_minute=1000)
            await asyncio.gather(*(client.get("items", {"page": i}) for i in range(20)))

    assert peak == 5
    assert client.stats.peak_in_flight == 5


# --- upstream failures --------------------------------------------------------------------


@respx.mock
async def test_server_errors_retry_reads_then_give_up(client, ft):
    route = respx.get(f"{BASE}/items").mock(return_value=zoho_error(503, 0))
    with pytest.raises(UpstreamUnavailable):
        await client.get("items")
    assert route.call_count == 4  # 1 try + 3 retries
    assert ft.sleeps == [1.0, 2.0, 4.0]


@respx.mock
async def test_network_errors_are_retried(client):
    route = respx.get(f"{BASE}/items").mock(side_effect=[httpx.ConnectTimeout("boom"), ok({"items": []})])
    await client.get("items")
    assert route.call_count == 2


@respx.mock
async def test_writes_are_not_retried_on_server_error(client):
    route = respx.post(f"{BASE}/items").mock(return_value=zoho_error(503, 0))
    with pytest.raises(UpstreamUnavailable):
        await client.post("items", {"name": "x"})
    assert route.call_count == 1


# --- auth ------------------------------------------------------------------------------------


@respx.mock
async def test_401_refreshes_token_once_and_retries(client):
    route = respx.get(f"{BASE}/items").mock(side_effect=[zoho_error(401, 57), ok({"items": []})])
    await client.get("items")
    assert route.calls[1].request.headers["Authorization"] == "Zoho-oauthtoken t2"
    assert client._tokens.refreshes == 1


@respx.mock
async def test_repeated_401_becomes_permission_denied(client):
    respx.get(f"{BASE}/items").mock(return_value=zoho_error(401, 57))
    with pytest.raises(PermissionDenied):
        await client.get("items")
    assert client._tokens.refreshes == 1


# --- cache -------------------------------------------------------------------------------------


@respx.mock
async def test_identical_concurrent_calls_share_one_request(client):
    route = respx.get(f"{BASE}/items").mock(return_value=ok({"items": []}))
    results = await asyncio.gather(*(client.get("items", {"q": "a"}, cache_ttl=30) for _ in range(10)))
    assert route.call_count == 1
    assert sum(r.cached for r in results) == 9


@respx.mock
async def test_cache_expires_and_fresh_bypasses(client, ft):
    route = respx.get(f"{BASE}/items").mock(return_value=ok({"items": []}))
    await client.get("items", cache_ttl=30)
    assert (await client.get("items", cache_ttl=30)).cached
    assert not (await client.get("items", cache_ttl=30, fresh=True)).cached
    ft.now += 31
    assert not (await client.get("items", cache_ttl=30)).cached
    assert route.call_count == 3


@respx.mock
async def test_errors_are_not_cached(client):
    route = respx.get(f"{BASE}/items/1").mock(side_effect=[zoho_error(404, 1002), ok({"item": {}})])
    with pytest.raises(NotFound):
        await client.get("items/1", cache_ttl=30)
    assert not (await client.get("items/1", cache_ttl=30)).cached
    assert route.call_count == 2


# --- pagination --------------------------------------------------------------------------------


def page(n: int, more: bool) -> httpx.Response:
    return ok({"items": [{"item_id": str(n)}], "page_context": {"page": n, "has_more_page": more}})


@respx.mock
async def test_paginate_stops_at_last_page(client):
    respx.get(f"{BASE}/items").mock(side_effect=[page(1, True), page(2, False)])
    records, truncated = await client.paginate("items", "items")
    assert [r["item_id"] for r in records] == ["1", "2"]
    assert not truncated


@respx.mock
async def test_paginate_caps_pages_and_reports_truncation(client):
    route = respx.get(f"{BASE}/items").mock(side_effect=[page(n, True) for n in range(1, 10)])
    records, truncated = await client.paginate("items", "items", max_pages=3)
    assert len(records) == 3 and truncated
    assert route.call_count == 3


@respx.mock
async def test_status_reports_session_stats(client):
    respx.get(f"{BASE}/items").mock(
        return_value=ok(
            {"items": []},
            **{"x-rate-limit-limit": "7500", "x-rate-limit-remaining": "7400", "x-rate-limit-reset": "60"},
        )
    )
    await client.get("items", cache_ttl=30)
    await client.get("items", cache_ttl=30)
    status = client.status()
    assert status["organization"] == {"id": ORG, "name": "Demo Kurta Store"}
    assert status["daily_quota"]["remaining"] == 7400
    assert status["session"]["upstream_requests"] == 1
    assert status["session"]["cache_hits"] == 1
