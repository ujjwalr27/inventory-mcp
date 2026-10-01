import re
from typing import Literal

from zoho_inventory_mcp.client.errors import NotFound
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.models import Contact
from zoho_inventory_mcp.resources import (
    DETAIL_TTL,
    SEARCH_TTL,
    InvalidArgument,
    Page,
    clamp_limit,
    has_more,
    page_from_cursor,
)

ContactType = Literal["customers", "vendors", "all"]
_TYPE_FILTER = {"customers": "Status.Customers", "vendors": "Status.Vendors", "all": "Status.All"}

# Zoho's phone_contains is a plain substring match on the stored text ("+91 55500 01001"),
# so "5550001001" or "+915550001001" would miss. We search on the last 5 digits and then
# compare digits-only on our side.
_PHONE_SEARCH_DIGITS = 5


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


async def search_contacts(
    client: ZohoClient,
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    contact_type: ContactType = "customers",
    limit: int = 10,
    cursor: str | None = None,
    fresh: bool = False,
) -> Page[Contact]:
    page = page_from_cursor(cursor)
    phone_digits = _digits(phone)
    if phone and len(phone_digits) < _PHONE_SEARCH_DIGITS:
        raise InvalidArgument(f"phone must contain at least {_PHONE_SEARCH_DIGITS} digits.")

    fetched = await client.get(
        "contacts",
        {
            "contact_name_contains": (name or "").strip() or None,
            "email_contains": (email or "").strip() or None,
            "phone_contains": phone_digits[-_PHONE_SEARCH_DIGITS:] or None,
            "filter_by": _TYPE_FILTER[contact_type],
            "page": page,
            "per_page": clamp_limit(limit),
        },
        cache_ttl=SEARCH_TTL,
        fresh=fresh,
    )
    rows = fetched.data.get("contacts", [])
    if phone_digits:
        # Last 10 digits drops a country code the caller may or may not have included.
        wanted = phone_digits[-10:]
        rows = [r for r in rows if any(wanted in _digits(r.get(f)) for f in ("mobile", "phone"))]
    return Page([Contact.from_zoho(r) for r in rows], page, has_more(fetched.data), fetched.cached)


async def get_contact(client: ZohoClient, *, contact_id: str, fresh: bool = False) -> tuple[Contact | None, bool]:
    if not contact_id:
        raise InvalidArgument("Provide contact_id.")
    try:
        fetched = await client.get(f"contacts/{contact_id}", cache_ttl=DETAIL_TTL, fresh=fresh)
    except NotFound:
        return None, False
    return Contact.from_zoho(fetched.data["contact"]), fetched.cached
