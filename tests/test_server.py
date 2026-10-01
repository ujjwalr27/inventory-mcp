"""End-to-end tool tests: MCP call_tool -> resources -> ZohoClient -> mocked Zoho (real fixtures)."""

import json

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from zoho_inventory_mcp.auth.oauth import NotAuthenticatedError
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.http_auth import BearerTokenMiddleware
from zoho_inventory_mcp.server import create_server

from helpers import BASE, FakeTokens, fixture, ok, zoho_error

KURTA_ID = fixture("item_detail")["item"]["item_id"]
PRIYA = fixture("contact_detail")["contact"]
SO_SHIPPED = fixture("salesorder_detail_shipped")["salesorder"]


@pytest.fixture
async def zoho():
    async with httpx.AsyncClient() as http:
        yield ZohoClient(FakeTokens(), http, per_minute=1000)


@pytest.fixture
def server(zoho):
    async def get_client():
        return zoho

    return create_server(get_client)


async def call(server, name: str, args: dict | None = None) -> dict:
    result = await server.call_tool(name, args or {})
    assert not result.is_error
    return result.structured_content


async def call_error(server, name: str, args: dict | None = None) -> dict:
    with pytest.raises(ToolError) as exc:
        await server.call_tool(name, args or {})
    message = str(exc.value)
    return json.loads(message[message.index("{"):])["error"]


# --- items -----------------------------------------------------------------------------


@respx.mock
async def test_search_items_returns_trimmed_page(server):
    route = respx.get(f"{BASE}/items").mock(return_value=ok(fixture("items_search_kurta")))
    out = await call(server, "search_items", {"query": "kurta", "limit": 5})

    params = route.calls.last.request.url.params
    assert params["search_text"] == "kurta" and params["filter_by"] == "Status.Active"
    assert params["per_page"] == "5"
    assert out["count"] == 3
    assert {r["sku"] for r in out["results"]} == {"DEMO-KUR-BLU-M", "DEMO-KUR-BLU-L", "DEMO-KUR-MUS-M"}
    assert out["next_cursor"] is None
    assert out["organization"] == "Demo Kurta Store"
    assert out["warnings"] == []


@respx.mock
async def test_get_item_by_sku_resolves_then_fetches_detail(server):
    respx.get(f"{BASE}/items", params={"sku": "DEMO-KUR-BLU-M"}).mock(
        return_value=ok({"items": [{"item_id": KURTA_ID}]})
    )
    respx.get(f"{BASE}/items/{KURTA_ID}").mock(return_value=ok(fixture("item_detail")))
    out = await call(server, "get_item", {"sku": "DEMO-KUR-BLU-M"})
    assert out["found"] is True
    assert out["item"]["committed_stock"] == 1


@respx.mock
async def test_get_item_unknown_id_is_found_false_not_an_error(server):
    respx.get(f"{BASE}/items/999").mock(return_value=zoho_error(404, 1002, "not available"))
    out = await call(server, "get_item", {"item_id": "999"})
    assert out["found"] is False and "search_items" in out["hint"]


async def test_get_item_needs_an_identifier(server):
    error = await call_error(server, "get_item", {})
    assert error["type"] == "invalid_request"


@respx.mock
async def test_low_stock_uses_native_zoho_view(server):
    route = respx.get(f"{BASE}/items").mock(return_value=ok(fixture("items_lowstock")))
    out = await call(server, "list_low_stock_items")
    assert route.calls.last.request.url.params["filter_by"] == "Status.Lowstock"
    assert out["count"] == 5 and all(r["is_low_stock"] for r in out["results"])


# --- sales orders ------------------------------------------------------------------------


@respx.mock
async def test_order_number_falls_back_to_reference_number(server):
    rows = fixture("salesorders_list")["salesorders"]
    so4 = next(r for r in rows if r["reference_number"] == "DEMO-SO-04")
    respx.get(f"{BASE}/salesorders", params={"salesorder_number": "DEMO-SO-04"}).mock(
        return_value=ok({"salesorders": []})
    )
    respx.get(f"{BASE}/salesorders", params={"reference_number": "DEMO-SO-04"}).mock(
        return_value=ok({"salesorders": [so4]})
    )
    out = await call(server, "search_sales_orders", {"order_number": "DEMO-SO-04"})
    assert out["results"][0]["fulfillment_stage"] == "packed"


@respx.mock
async def test_open_ended_date_range_and_status_filter(server):
    route = respx.get(f"{BASE}/salesorders").mock(return_value=ok({"salesorders": []}))
    await call(server, "search_sales_orders", {"date_from": "2026-09-28", "status": "shipped"})
    params = route.calls.last.request.url.params
    assert (params["date_start"], params["date_end"]) == ("2026-09-28", "2099-12-31")
    assert params["filter_by"] == "Status.Shipped"


async def test_bad_date_is_reported_as_fixable_input_error(server):
    error = await call_error(server, "search_sales_orders", {"date_from": "last week"})
    assert error["type"] == "invalid_request" and "YYYY-MM-DD" in error["detail"]


async def test_unknown_status_rejected_by_schema(server):
    with pytest.raises(ToolError):
        await server.call_tool("search_sales_orders", {"status": "packed"})


@respx.mock
async def test_get_sales_order_shows_tracking(server):
    respx.get(f"{BASE}/salesorders/{SO_SHIPPED['salesorder_id']}").mock(
        return_value=ok(fixture("salesorder_detail_shipped"))
    )
    out = await call(server, "get_sales_order", {"salesorder_id": SO_SHIPPED["salesorder_id"]})
    order = out["sales_order"]
    assert order["fulfillment_stage"] == "shipped"
    assert order["shipments"][0]["tracking_number"] == "DEMO-TRK-01"
    assert "paid_status" not in order and "balance" not in order


# --- contacts ----------------------------------------------------------------------------


@respx.mock
async def test_phone_search_normalises_formats(server):
    route = respx.get(f"{BASE}/contacts").mock(return_value=ok(fixture("contacts_list")))
    out = await call(server, "search_contacts", {"phone": "+91-5550001003"})
    assert route.calls.last.request.url.params["phone_contains"] == "01003"
    assert [r["name"] for r in out["results"]] == ["Ananya Iyer"]


async def test_phone_search_needs_enough_digits(server):
    error = await call_error(server, "search_contacts", {"phone": "123"})
    assert error["type"] == "invalid_request"


@respx.mock
async def test_get_contact(server):
    respx.get(f"{BASE}/contacts/{PRIYA['contact_id']}").mock(return_value=ok(fixture("contact_detail")))
    out = await call(server, "get_contact", {"contact_id": PRIYA["contact_id"]})
    assert out["contact"]["location"] == "Jaipur, Rajasthan"


# --- errors, quota, status ----------------------------------------------------------------


@respx.mock
async def test_daily_limit_reaches_agent_as_non_retryable_error(server):
    respx.get(f"{BASE}/items").mock(
        return_value=zoho_error(429, 45, "limit exceeded", **{"x-rate-limit-reset": "3600"})
    )
    error = await call_error(server, "search_items", {"query": "kurta"})
    assert error["type"] == "daily_limit_exceeded"
    assert error["retryable"] is False and error["retry_after_seconds"] == 3600


@respx.mock
async def test_low_quota_adds_warning(server):
    respx.get(f"{BASE}/items").mock(
        return_value=ok(
            {"items": []},
            **{"x-rate-limit-limit": "1000", "x-rate-limit-remaining": "40", "x-rate-limit-reset": "600"},
        )
    )
    out = await call(server, "search_items", {"query": "x"})
    assert "40 calls left" in out["warnings"][0]


async def test_not_connected_is_explained():
    async def get_client():
        raise NotAuthenticatedError("Not connected to Zoho.")

    error = await call_error(create_server(get_client), "search_items", {"query": "x"})
    assert error["type"] == "not_connected" and error["retryable"] is False


@respx.mock
async def test_connector_status_makes_no_zoho_call(server):
    route = respx.route()
    out = await call(server, "get_connector_status")
    assert not route.called
    assert out["connected"] and out["read_only"]
    assert out["organization"]["name"] == "Demo Kurta Store"


async def test_every_tool_is_declared_read_only(server):
    tools = await server.list_tools()
    assert len(tools) == 8
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


# --- HTTP bearer gate ----------------------------------------------------------------------


async def test_bearer_middleware():
    inner = Starlette(routes=[Route("/mcp", lambda request: PlainTextResponse("ok"))])
    app = BearerTokenMiddleware(inner, "s3cret-token-0123456789")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/mcp")).status_code == 401
        assert (await http.get("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        good = await http.get("/mcp", headers={"Authorization": "Bearer s3cret-token-0123456789"})
        assert good.status_code == 200 and good.text == "ok"


def test_bearer_middleware_rejects_weak_token():
    with pytest.raises(ValueError):
        BearerTokenMiddleware(Starlette(), "short")
