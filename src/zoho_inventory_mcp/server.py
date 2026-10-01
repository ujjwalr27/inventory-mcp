"""MCP server exposing read-only Zoho Inventory tools to an Agent Studio agent."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from zoho_inventory_mcp import __version__
from zoho_inventory_mcp.auth.oauth import NotAuthenticatedError, TokenProvider
from zoho_inventory_mcp.auth.token_store import TokenStore
from zoho_inventory_mcp.client.errors import BadRequest, ZohoError
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.config import Settings
from zoho_inventory_mcp.resources import InvalidArgument, Page
from zoho_inventory_mcp.resources import contacts as contacts_api
from zoho_inventory_mcp.resources import items as items_api
from zoho_inventory_mcp.resources import sales_orders as orders_api
from zoho_inventory_mcp.resources.contacts import ContactType
from zoho_inventory_mcp.resources.items import ItemStatus
from zoho_inventory_mcp.resources.sales_orders import OrderStatus

ClientFactory = Callable[[], Awaitable[ZohoClient]]

INSTRUCTIONS = """\
Read-only access to one merchant's Zoho Inventory: products and stock levels, sales orders \
with their fulfillment/shipping progress, and customer contact details.

You can look things up; you cannot create, edit, cancel or delete anything, and you cannot \
see invoices, payments, refunds or purchase orders. If the user asks for a change, say it must \
be done in Zoho Inventory directly.

Tips: search first, then call get_* with the returned ID for full details. Results are cached \
for up to a minute; pass fresh=true only when the user explicitly asks for the latest data. \
Each tool result may include `warnings` (for example a nearly exhausted daily API quota) that \
you should pass on to the user. If a tool returns an error with retryable=false, do not retry it.\
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

Limit = Annotated[int, Field(ge=1, le=50, description="Maximum results to return (1-50).")]
Cursor = Annotated[str | None, Field(description="Pass next_cursor from a previous result to get the next page.")]
Fresh = Annotated[bool, Field(description="Bypass the short-lived cache. Use only when the user asks for live data.")]


def _meta(client: ZohoClient, cached: bool) -> dict[str, Any]:
    warnings = []
    if client.daily.low:
        warnings.append(
            f"Zoho daily API quota is nearly used up ({client.daily.remaining} calls left). "
            "Avoid unnecessary lookups."
        )
    return {
        "organization": client.organization_name,
        "cached": cached,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warnings": warnings,
    }


def _page(client: ZohoClient, page: Page) -> dict[str, Any]:
    return {
        "results": [r.model_dump() for r in page.results],
        "count": len(page.results),
        "next_cursor": page.next_cursor,
        **_meta(client, page.cached),
    }


def _single(client: ZohoClient, key: str, record: Any, cached: bool, hint: str) -> dict[str, Any]:
    if record is None:
        return {"found": False, key: None, "hint": hint, **_meta(client, cached)}
    return {"found": True, key: record.model_dump(), **_meta(client, cached)}


def create_server(get_client: ClientFactory) -> MCPServer:
    mcp = MCPServer(name="zoho-inventory", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

    async def run(call: Callable[[ZohoClient], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Run a tool body, turning failures into structured errors the agent can act on."""
        try:
            return await call(await get_client())
        except InvalidArgument as exc:
            error = BadRequest(str(exc))
            error.agent_hint = "The tool arguments were invalid. Fix them using `detail` and call again."
            raise ToolError(json.dumps(error.to_agent())) from None
        except ZohoError as exc:
            raise ToolError(json.dumps(exc.to_agent())) from None
        except NotAuthenticatedError as exc:
            payload = {
                "error": {
                    "type": "not_connected",
                    "retryable": False,
                    "retry_after_seconds": None,
                    "message": "The merchant has not connected Zoho Inventory (or revoked access). "
                    "They need to reconnect before inventory data is available.",
                    "detail": str(exc),
                }
            }
            raise ToolError(json.dumps(payload)) from None

    # --- items -------------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def search_items(
        query: Annotated[
            str | None, Field(description="Words from the product name or SKU, e.g. 'blue kurta' or 'DEMO-TEA'.")
        ] = None,
        sku: Annotated[str | None, Field(description="Exact SKU, when the user gives one.")] = None,
        status: Annotated[ItemStatus, Field(description="Product status filter.")] = "active",
        limit: Limit = 10,
        cursor: Cursor = None,
        fresh: Fresh = False,
    ) -> dict[str, Any]:
        """Find products by name or SKU and see their price and stock.

        Use for "do we have X?", "how many X are left?", "what does X cost?". With no query,
        lists products. For a single product's committed stock, follow up with get_item.
        """
        return await run(
            lambda c: _wrap_page(c, items_api.search_items(c, query=query, sku=sku, status=status, limit=limit, cursor=cursor, fresh=fresh))
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_item(
        item_id: Annotated[str | None, Field(description="item_id from search_items.")] = None,
        sku: Annotated[str | None, Field(description="Exact SKU, if the item_id is not known.")] = None,
        fresh: Fresh = False,
    ) -> dict[str, Any]:
        """Full stock picture for one product: on hand, committed to orders, available for sale, reorder level."""

        async def body(c: ZohoClient) -> dict[str, Any]:
            item, cached = await items_api.get_item(c, item_id=item_id, sku=sku, fresh=fresh)
            return _single(c, "item", item, cached, "No product with that ID/SKU. Try search_items.")

        return await run(body)

    @mcp.tool(annotations=READ_ONLY)
    async def list_low_stock_items(limit: Limit = 20, cursor: Cursor = None, fresh: Fresh = False) -> dict[str, Any]:
        """Products at or below their reorder level, i.e. what the merchant should restock.

        Uses Zoho's own low-stock view. Items with no reorder level set never appear here.
        """
        return await run(lambda c: _wrap_page(c, items_api.list_low_stock_items(c, limit=limit, cursor=cursor, fresh=fresh)))

    # --- sales orders ------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def search_sales_orders(
        order_number: Annotated[
            str | None, Field(description="Zoho order number (SO-00001) or the merchant's reference number.")
        ] = None,
        customer_name: Annotated[str | None, Field(description="Customer's name as stored in Zoho.")] = None,
        customer_id: Annotated[str | None, Field(description="contact_id from search_contacts.")] = None,
        status: Annotated[
            OrderStatus | None,
            Field(description="'confirmed' also includes packed and shipped orders; check fulfillment_stage."),
        ] = None,
        date_from: Annotated[str | None, Field(description="Order date on or after, YYYY-MM-DD.")] = None,
        date_to: Annotated[str | None, Field(description="Order date on or before, YYYY-MM-DD.")] = None,
        limit: Limit = 10,
        cursor: Cursor = None,
        fresh: Fresh = False,
    ) -> dict[str, Any]:
        """Find sales orders by number, customer, status or date range (newest first).

        Each result has fulfillment_stage: draft, confirmed, packed, shipped, etc. Use
        get_sales_order for line items and tracking numbers.
        """
        return await run(
            lambda c: _wrap_page(
                c,
                orders_api.search_sales_orders(
                    c,
                    order_number=order_number,
                    customer_name=customer_name,
                    customer_id=customer_id,
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                    cursor=cursor,
                    fresh=fresh,
                ),
            )
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_sales_order(
        salesorder_id: Annotated[str | None, Field(description="salesorder_id from search_sales_orders.")] = None,
        order_number: Annotated[str | None, Field(description="SO number or reference, if the ID is not known.")] = None,
        fresh: Fresh = False,
    ) -> dict[str, Any]:
        """One order in full: what was bought, how much is packed/shipped, carrier and tracking number.

        Use for "what did they order?", "has it shipped?", "where is my order?".
        """

        async def body(c: ZohoClient) -> dict[str, Any]:
            order, cached = await orders_api.get_sales_order(
                c, salesorder_id=salesorder_id, order_number=order_number, fresh=fresh
            )
            return _single(c, "sales_order", order, cached, "No order with that ID/number. Try search_sales_orders.")

        return await run(body)

    # --- contacts ----------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def search_contacts(
        name: Annotated[str | None, Field(description="Part of the customer's name.")] = None,
        email: Annotated[str | None, Field(description="Part of the email address.")] = None,
        phone: Annotated[str | None, Field(description="Phone number, any format; at least 5 digits.")] = None,
        contact_type: Annotated[ContactType, Field(description="Which contacts to search.")] = "customers",
        limit: Limit = 10,
        cursor: Cursor = None,
        fresh: Fresh = False,
    ) -> dict[str, Any]:
        """Find a customer by name, email or phone to identify who is asking or get their contact_id."""
        return await run(
            lambda c: _wrap_page(
                c,
                contacts_api.search_contacts(
                    c, name=name, email=email, phone=phone, contact_type=contact_type, limit=limit, cursor=cursor, fresh=fresh
                ),
            )
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_contact(
        contact_id: Annotated[str, Field(description="contact_id from search_contacts.")],
        fresh: Fresh = False,
    ) -> dict[str, Any]:
        """One customer's details: name, company, email, phone and city/state. No payment information."""

        async def body(c: ZohoClient) -> dict[str, Any]:
            contact, cached = await contacts_api.get_contact(c, contact_id=contact_id, fresh=fresh)
            return _single(c, "contact", contact, cached, "No contact with that ID. Try search_contacts.")

        return await run(body)

    # --- connector -----------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def get_connector_status() -> dict[str, Any]:
        """Which Zoho organization is connected, and how much of today's API quota is left.

        Call this if tools are failing or the user asks whether the inventory connection works.
        It does not call Zoho, so it is free.
        """
        return await run(_status)

    return mcp


async def _wrap_page(client: ZohoClient, pending: Awaitable[Page]) -> dict[str, Any]:
    return _page(client, await pending)


async def _status(client: ZohoClient) -> dict[str, Any]:
    return {"connected": True, "read_only": True, **client.status()}


class ConnectorRuntime:
    """Builds the Zoho client lazily, on the first tool call, inside the server's event loop."""

    def __init__(self, settings: Settings, profile: str = "connector"):
        self._settings = settings
        self._profile = profile
        self._client: ZohoClient | None = None
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def get_client(self) -> ZohoClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
                    try:
                        tokens = TokenProvider(
                            TokenStore(self._settings.token_dir, self._profile),
                            http,
                            client_id=self._settings.client_id,
                            client_secret=self._settings.client_secret.get_secret_value(),
                        )
                    except NotAuthenticatedError:
                        await http.aclose()
                        raise  # not cached: the merchant may log in while the server runs
                    self._http = http
                    self._client = ZohoClient(tokens, http, plan=self._settings.plan)
        return self._client

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
