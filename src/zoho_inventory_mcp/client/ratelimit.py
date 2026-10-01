"""Client-side pacing so we stay under Zoho's limits instead of discovering them via 429s."""

import time
from collections.abc import Callable, Mapping

from zoho_inventory_mcp.client.errors import DailyLimitExceeded

Clock = Callable[[], float]

# Fallback daily quotas by plan, used only until Zoho's x-rate-limit-* headers arrive.
PLAN_DAILY_LIMITS = {"free": 1000, "standard": 2000, "professional": 5000, "premium": 10000, "enterprise": 10000}
PLAN_CONCURRENCY = {"free": 5, "standard": 10, "professional": 10, "premium": 10, "enterprise": 10}


class MinuteBucket:
    """Reservation-based token bucket for Zoho's 100 requests/minute/org limit.

    Each caller books the next free slot and is told how long to sleep, so we know a
    caller's wait *before* it starts waiting. That lets the client fail fast with a clear
    "retry in N seconds" instead of silently stalling an agent's tool call for minutes.

    Defaults to 90/min to leave headroom for the merchant's other integrations, which
    share the same org-wide quota.
    """

    def __init__(self, per_minute: int = 90, *, clock: Clock = time.monotonic):
        self.capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._tokens = self.capacity  # may go negative: that is debt owed by booked callers
        self._clock = clock
        self._updated = clock()  # may sit in the future after a penalty

    def _refill(self, now: float) -> None:
        if now > self._updated:
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now

    def reserve(self, max_wait: float | None = None) -> tuple[bool, float]:
        """Book one request slot. Returns (granted, seconds_to_wait).

        If the wait would exceed `max_wait` nothing is booked and granted is False.
        """
        now = self._clock()
        self._refill(now)
        after = self._tokens - 1
        delay = (max(now, self._updated) - now) + max(0.0, -after) / self._rate
        if max_wait is not None and delay > max_wait:
            return False, delay
        self._tokens = after
        return True, delay

    def penalize(self, seconds: float) -> None:
        """Zoho said we exceeded the minute limit anyway: hand out no new slots for `seconds`."""
        now = self._clock()
        self._refill(now)
        # Leave exactly one token, so the first caller goes the moment the penalty ends.
        self._tokens = min(self._tokens, 1.0)
        self._updated = max(self._updated, now + seconds)


class DailyBudget:
    """Tracks the org's daily quota, preferring Zoho's own x-rate-limit-* headers.

    When the quota is known to be exhausted we fail locally instead of spending a request
    to be told so again.
    """

    WARN_FRACTION = 0.1

    def __init__(self, plan: str = "free", *, clock: Clock = time.time):
        self.limit: int = PLAN_DAILY_LIMITS.get(plan, 1000)
        self.remaining: int | None = None  # unknown until the first response
        self.reset_at: float | None = None
        self._clock = clock

    def check(self) -> None:
        if self.remaining is not None and self.remaining <= 0:
            now = self._clock()
            if self.reset_at is None or now < self.reset_at:
                retry_after = None if self.reset_at is None else self.reset_at - now
                raise DailyLimitExceeded("Daily API quota exhausted (known locally).", retry_after=retry_after)
            self.remaining = None  # reset time passed; let the next response tell us

    def record(self, headers: Mapping[str, str]) -> None:
        try:
            self.limit = int(headers["x-rate-limit-limit"])
            self.remaining = int(headers["x-rate-limit-remaining"])
            self.reset_at = self._clock() + float(headers["x-rate-limit-reset"])
        except (KeyError, ValueError):
            if self.remaining is not None:
                self.remaining -= 1

    def mark_exhausted(self, retry_after: float | None) -> None:
        self.remaining = 0
        if retry_after is not None:
            self.reset_at = self._clock() + retry_after

    @property
    def low(self) -> bool:
        return self.remaining is not None and self.remaining < self.limit * self.WARN_FRACTION

    def snapshot(self) -> dict:
        reset_in = None if self.reset_at is None else max(0, round(self.reset_at - self._clock()))
        return {"limit": self.limit, "remaining": self.remaining, "resets_in_seconds": reset_in}
