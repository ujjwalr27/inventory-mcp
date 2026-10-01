"""Read-only access to Zoho Inventory resources, returning trimmed models.

Search and filter parameters used here were verified against the live API
(see IMPLEMENTATION.md, P4).
"""

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

SEARCH_TTL = 30.0  # seconds; searches go stale faster than single records matter
DETAIL_TTL = 60.0
MAX_LIMIT = 50


@dataclass(frozen=True)
class Page(Generic[T]):
    results: list[T]
    page: int
    has_more: bool
    cached: bool

    @property
    def next_cursor(self) -> str | None:
        return str(self.page + 1) if self.has_more else None


class InvalidArgument(ValueError):
    """Bad tool input that the agent can fix (reported back as invalid_request)."""


def page_from_cursor(cursor: str | None) -> int:
    if cursor in (None, ""):
        return 1
    try:
        page = int(cursor)
    except ValueError:
        raise InvalidArgument("cursor must be the next_cursor value from a previous result.") from None
    if page < 1:
        raise InvalidArgument("cursor must be the next_cursor value from a previous result.")
    return page


def clamp_limit(limit: int) -> int:
    return max(1, min(MAX_LIMIT, limit))


def has_more(data: dict) -> bool:
    return bool(data.get("page_context", {}).get("has_more_page"))
