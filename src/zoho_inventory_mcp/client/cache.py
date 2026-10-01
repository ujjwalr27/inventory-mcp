"""Short-lived response cache with single-flight de-duplication.

Agents often ask the same thing twice in one conversation ("is it in stock?" ... "and
what's the price?"). Caching for tens of seconds saves quota without serving stale data
for long, and single-flight means N identical concurrent calls cost one Zoho request.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


class ResponseCache:
    def __init__(self, maxsize: int = 512, *, clock: Callable[[], float] = time.monotonic):
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._maxsize = maxsize
        self._clock = clock
        self.hits = 0

    def _lookup(self, key: str) -> tuple[bool, Any]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if self._clock() >= expires:
            del self._entries[key]
            return False, None
        self._entries.move_to_end(key)
        return True, value

    async def get_or_fetch(
        self, key: str, ttl: float, fetch: Callable[[], Awaitable[Any]], *, fresh: bool = False
    ) -> tuple[Any, bool]:
        """Return (value, served_from_cache). Errors are never cached."""
        if not fresh:
            found, value = self._lookup(key)
            if found:
                self.hits += 1
                return value, True
            if key in self._inflight:
                self.hits += 1
                return await asyncio.shield(self._inflight[key]), True

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            value = await fetch()
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved so lone failures don't log warnings
            raise
        else:
            future.set_result(value)
            if ttl > 0:
                self._entries[key] = (self._clock() + ttl, value)
                self._entries.move_to_end(key)
                while len(self._entries) > self._maxsize:
                    self._entries.popitem(last=False)
            return value, False
        finally:
            if self._inflight.get(key) is future:
                del self._inflight[key]
