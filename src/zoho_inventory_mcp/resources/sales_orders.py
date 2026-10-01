from datetime import date
from typing import Literal

from zoho_inventory_mcp.client.errors import NotFound
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.models import SalesOrder, SalesOrderSummary
from zoho_inventory_mcp.resources import (
    DETAIL_TTL,
    SEARCH_TTL,
    InvalidArgument,
    Page,
    clamp_limit,
    has_more,
    page_from_cursor,
)

# Only filters verified against the live API. Zoho's "confirmed" view also includes
# packed and shipped orders; fulfillment_stage on each result tells them apart.
OrderStatus = Literal["draft", "confirmed", "shipped"]
_STATUS_FILTER = {"draft": "Status.Draft", "confirmed": "Status.Confirmed", "shipped": "Status.Shipped"}

_EARLIEST = "2000-01-01"
_LATEST = "2099-12-31"


def _iso(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise InvalidArgument(f"{field} must be a date in YYYY-MM-DD format.") from None


async def search_sales_orders(
    client: ZohoClient,
    *,
    order_number: str | None = None,
    customer_name: str | None = None,
    customer_id: str | None = None,
    status: OrderStatus | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10,
    cursor: str | None = None,
    fresh: bool = False,
) -> Page[SalesOrderSummary]:
    page = page_from_cursor(cursor)
    start, end = _iso(date_from, "date_from"), _iso(date_to, "date_to")
    if start and end and start > end:
        raise InvalidArgument("date_from must be on or before date_to.")

    params = {
        "customer_name": (customer_name or "").strip() or None,
        "customer_id": customer_id,
        "filter_by": _STATUS_FILTER[status] if status else None,
        # Zoho needs both ends of the range, so open-ended ranges get a far bound.
        "date_start": (start or _EARLIEST) if (start or end) else None,
        "date_end": (end or _LATEST) if (start or end) else None,
        "page": page,
        "per_page": clamp_limit(limit),
    }

    number = (order_number or "").strip()
    if number:
        # Merchants quote either Zoho's number (SO-00001) or their own reference (DEMO-SO-01).
        fetched = await client.get("salesorders", {**params, "salesorder_number": number}, cache_ttl=SEARCH_TTL, fresh=fresh)
        if not fetched.data.get("salesorders"):
            fetched = await client.get(
                "salesorders", {**params, "reference_number": number}, cache_ttl=SEARCH_TTL, fresh=fresh
            )
    else:
        fetched = await client.get("salesorders", params, cache_ttl=SEARCH_TTL, fresh=fresh)

    orders = [SalesOrderSummary.from_zoho(raw) for raw in fetched.data.get("salesorders", [])]
    return Page(orders, page, has_more(fetched.data), fetched.cached)


async def get_sales_order(
    client: ZohoClient, *, salesorder_id: str | None = None, order_number: str | None = None, fresh: bool = False
) -> tuple[SalesOrder | None, bool]:
    if not salesorder_id and not order_number:
        raise InvalidArgument("Provide salesorder_id or order_number.")
    lookup_cached = True
    if not salesorder_id:
        matches = await search_sales_orders(client, order_number=order_number, limit=1, fresh=fresh)
        if not matches.results:
            return None, matches.cached
        salesorder_id = matches.results[0].salesorder_id
        lookup_cached = matches.cached
    try:
        fetched = await client.get(f"salesorders/{salesorder_id}", cache_ttl=DETAIL_TTL, fresh=fresh)
    except NotFound:
        return None, False
    return SalesOrder.from_zoho(fetched.data["salesorder"]), fetched.cached and lookup_cached
