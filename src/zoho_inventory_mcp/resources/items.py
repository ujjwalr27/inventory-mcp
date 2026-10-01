from typing import Literal

from zoho_inventory_mcp.client.errors import NotFound
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.models import Item
from zoho_inventory_mcp.resources import (
    DETAIL_TTL,
    SEARCH_TTL,
    InvalidArgument,
    Page,
    clamp_limit,
    has_more,
    page_from_cursor,
)

ItemStatus = Literal["active", "inactive", "all"]
_STATUS_FILTER = {"active": "Status.Active", "inactive": "Status.Inactive", "all": "Status.All"}


async def search_items(
    client: ZohoClient,
    *,
    query: str | None = None,
    sku: str | None = None,
    status: ItemStatus = "active",
    limit: int = 10,
    cursor: str | None = None,
    fresh: bool = False,
) -> Page[Item]:
    page = page_from_cursor(cursor)
    fetched = await client.get(
        "items",
        {
            # Zoho's search_text matches both name and SKU.
            "search_text": (query or "").strip() or None,
            "sku": (sku or "").strip() or None,
            "filter_by": _STATUS_FILTER[status],
            "page": page,
            "per_page": clamp_limit(limit),
        },
        cache_ttl=SEARCH_TTL,
        fresh=fresh,
    )
    items = [Item.from_zoho(raw) for raw in fetched.data.get("items", [])]
    return Page(items, page, has_more(fetched.data), fetched.cached)


async def get_item(
    client: ZohoClient, *, item_id: str | None = None, sku: str | None = None, fresh: bool = False
) -> tuple[Item | None, bool]:
    """Return (item or None, served_from_cache)."""
    if not item_id and not sku:
        raise InvalidArgument("Provide item_id or sku.")
    cached_lookup = True
    if not item_id:
        matches = await client.get("items", {"sku": sku.strip()}, cache_ttl=SEARCH_TTL, fresh=fresh)
        rows = matches.data.get("items", [])
        if not rows:
            return None, matches.cached
        item_id = str(rows[0]["item_id"])
        cached_lookup = matches.cached
    try:
        fetched = await client.get(f"items/{item_id}", cache_ttl=DETAIL_TTL, fresh=fresh)
    except NotFound:
        return None, False
    return Item.from_zoho(fetched.data["item"]), fetched.cached and cached_lookup


async def list_low_stock_items(
    client: ZohoClient, *, limit: int = 20, cursor: str | None = None, fresh: bool = False
) -> Page[Item]:
    page = page_from_cursor(cursor)
    # Native Zoho view (verified live: returns exactly the items at/below reorder level).
    fetched = await client.get(
        "items",
        {"filter_by": "Status.Lowstock", "page": page, "per_page": clamp_limit(limit)},
        cache_ttl=SEARCH_TTL,
        fresh=fresh,
    )
    items = [Item.from_zoho(raw) for raw in fetched.data.get("items", [])]
    return Page(items, page, has_more(fetched.data), fetched.cached)
