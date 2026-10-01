"""Turns Zoho responses into typed errors that carry their own retry policy.

Zoho uses HTTP 429 for three different limits and tells them apart only by the `code`
in the body. Blindly retrying every 429 is wrong: a daily-limit 429 will not clear for
hours, and hammering after a per-minute 429 can get the org blocked.
"""

from typing import Any

import httpx

# Zoho body codes (see https://www.zoho.com/inventory/api/v1/introduction/)
CODE_RATE_LIMIT_MINUTE = 44
CODE_RATE_LIMIT_DAILY = 45
CODE_CONCURRENCY_LIMIT = 1070
CODE_NOT_FOUND = 1002


class ZohoError(Exception):
    kind = "upstream_error"
    retryable = False
    agent_hint = "Something went wrong talking to Zoho Inventory. Try again shortly."

    def __init__(
        self,
        message: str,
        *,
        zoho_code: int | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.zoho_code = zoho_code
        self.http_status = http_status
        self.retry_after = retry_after

    def to_agent(self) -> dict[str, Any]:
        """Structured error the MCP tool returns to the agent instead of a stack trace."""
        return {
            "error": {
                "type": self.kind,
                "retryable": self.retryable,
                "retry_after_seconds": None if self.retry_after is None else round(self.retry_after),
                "message": self.agent_hint,
                "detail": self.message,
            }
        }


class RateLimitedMinute(ZohoError):
    kind = "rate_limited"
    retryable = True
    agent_hint = "Zoho's per-minute API limit was reached. Wait about a minute before asking again."


class DailyLimitExceeded(ZohoError):
    kind = "daily_limit_exceeded"
    retryable = False
    agent_hint = (
        "This merchant's daily Zoho API quota is used up. Tell the user live inventory data is "
        "unavailable until the quota resets; do not keep retrying."
    )


class ConcurrencyLimited(ZohoError):
    kind = "concurrency_limited"
    retryable = True
    agent_hint = "Too many simultaneous requests to Zoho. Retry in a few seconds."


class AuthExpired(ZohoError):
    kind = "auth_expired"
    retryable = True  # internal: the client refreshes the token and retries once


class PermissionDenied(ZohoError):
    kind = "permission_denied"
    agent_hint = (
        "The connector is not authorized for this data. The merchant may have revoked access; "
        "they need to reconnect Zoho Inventory."
    )


class NotFound(ZohoError):
    kind = "not_found"
    agent_hint = "No matching record exists in Zoho Inventory. Check the ID or search instead."


class BadRequest(ZohoError):
    kind = "invalid_request"
    agent_hint = "Zoho rejected the request parameters. Adjust the filters and try again."


class UpstreamUnavailable(ZohoError):
    kind = "upstream_unavailable"
    retryable = True
    agent_hint = "Zoho Inventory is temporarily unavailable. Try again in a minute."


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _float_header(response: httpx.Response, name: str) -> float | None:
    try:
        return float(response.headers[name])
    except (KeyError, ValueError):
        return None


def classify(response: httpx.Response) -> ZohoError | None:
    """Return the error this response represents, or None if it is a success."""
    body = _body(response)
    code = body.get("code")
    message = str(body.get("message") or response.reason_phrase or "Zoho error")
    status = response.status_code
    common = {"zoho_code": code, "http_status": status}

    if status < 400 and code in (0, None):
        return None

    if status == 429 or code in (CODE_RATE_LIMIT_MINUTE, CODE_RATE_LIMIT_DAILY, CODE_CONCURRENCY_LIMIT):
        if code == CODE_CONCURRENCY_LIMIT:
            return ConcurrencyLimited(message, **common)
        # Codes 44 and 45 share the same message, so also trust the quota header.
        daily_exhausted = _float_header(response, "x-rate-limit-remaining") == 0
        if code == CODE_RATE_LIMIT_DAILY or daily_exhausted:
            return DailyLimitExceeded(
                message, retry_after=_float_header(response, "x-rate-limit-reset"), **common
            )
        return RateLimitedMinute(message, retry_after=_float_header(response, "retry-after"), **common)

    if status == 401:
        return AuthExpired(message, **common)
    if status == 403:
        return PermissionDenied(message, **common)
    if status == 404 or code == CODE_NOT_FOUND:
        return NotFound(message, **common)
    if status >= 500:
        return UpstreamUnavailable(message, **common)
    return BadRequest(message, **common)
