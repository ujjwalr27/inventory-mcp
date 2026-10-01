"""Zoho OAuth 2.0 (authorization code flow) plus automatic access-token refresh."""

import asyncio
import html
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import SecretStr

from zoho_inventory_mcp.auth.token_store import TokenSet, TokenStore
from zoho_inventory_mcp.regions import (
    UnknownRegionError,
    region_for_accounts_server,
    region_for_api_domain,
)

# The connector only ever asks for READ scopes, so even a bug cannot change merchant data.
READ_SCOPES: tuple[str, ...] = (
    "ZohoInventory.items.READ",
    "ZohoInventory.salesorders.READ",
    "ZohoInventory.contacts.READ",
    "ZohoInventory.settings.READ",  # needed for GET /organizations
)

# Used only by scripts/seed_demo_data.py, under a separate token profile.
SEED_SCOPES: tuple[str, ...] = READ_SCOPES + (
    "ZohoInventory.items.CREATE",
    "ZohoInventory.contacts.CREATE",
    "ZohoInventory.salesorders.CREATE",
    "ZohoInventory.salesorders.UPDATE",  # draft -> confirmed
    "ZohoInventory.packages.CREATE",
    "ZohoInventory.shipmentorders.CREATE",
)


class OAuthError(Exception):
    pass


class NotAuthenticatedError(OAuthError):
    pass


def authorize_url(
    *, accounts_server: str, client_id: str, redirect_uri: str, scopes: tuple[str, ...], state: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": ",".join(scopes),
        "redirect_uri": redirect_uri,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # always issue a fresh refresh token on re-login
        "state": state,
    }
    return f"{accounts_server}/oauth/v2/auth?{urlencode(params)}"


@dataclass(frozen=True)
class CallbackResult:
    code: str
    accounts_server: str | None  # set by Zoho when multi-DC is enabled
    location: str | None


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5">
<h2>{title}</h2><p>{body}</p></body></html>"""


def wait_for_callback(redirect_uri: str, expected_state: str, timeout: float = 300) -> CallbackResult:
    """Run a one-shot local HTTP server and return the authorization code Zoho redirects back with."""
    target = urlparse(redirect_uri)
    outcome: dict[str, CallbackResult | OAuthError] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request = urlparse(self.path)
            if request.path != target.path:
                self.send_error(404)  # e.g. /favicon.ico - keep waiting
                return
            query = {k: v[0] for k, v in parse_qs(request.query).items()}
            if "error" in query:
                outcome["result"] = OAuthError(f"Zoho returned an error: {query['error']}")
            elif query.get("state") != expected_state:
                outcome["result"] = OAuthError("State mismatch - possible CSRF, aborting login.")
            elif "code" not in query:
                outcome["result"] = OAuthError("Callback did not include an authorization code.")
            else:
                outcome["result"] = CallbackResult(
                    code=query["code"],
                    accounts_server=query.get("accounts-server"),
                    location=query.get("location"),
                )
            ok = isinstance(outcome["result"], CallbackResult)
            page = _PAGE.format(
                title="Connected" if ok else "Login failed",
                body="You can close this tab and return to the terminal."
                if ok
                else html.escape(str(outcome["result"])),
            )
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def log_message(self, *args: object) -> None:
            pass  # default logging would print the authorization code

    server = HTTPServer((target.hostname or "localhost", target.port or 80), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while "result" not in outcome:
            if time.monotonic() > deadline:
                raise OAuthError(f"Timed out after {timeout:.0f}s waiting for the Zoho redirect.")
            server.handle_request()
    finally:
        server.server_close()

    result = outcome["result"]
    if isinstance(result, OAuthError):
        raise result
    return result


def _parse_token_response(response: httpx.Response) -> dict:
    # Zoho reports OAuth failures as HTTP 200 with {"error": "..."}, so check the body too.
    try:
        payload = response.json()
    except ValueError:
        raise OAuthError(f"Unexpected token response (HTTP {response.status_code}).") from None
    if response.is_error or "error" in payload or "access_token" not in payload:
        raise OAuthError(f"Token request failed: {payload.get('error', response.status_code)}")
    return payload


def _resolve_api_domain(payload: dict, fallback: str) -> str:
    try:
        return region_for_api_domain(payload.get("api_domain") or fallback).api_domain
    except UnknownRegionError as exc:
        raise OAuthError(str(exc)) from None


async def exchange_code(
    http: httpx.AsyncClient,
    *,
    callback: CallbackResult,
    default_accounts_server: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> TokenSet:
    try:
        region = region_for_accounts_server(callback.accounts_server or default_accounts_server)
    except UnknownRegionError as exc:
        raise OAuthError(str(exc)) from None

    response = await http.post(
        f"{region.accounts_server}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "code": callback.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    payload = _parse_token_response(response)
    if "refresh_token" not in payload:
        raise OAuthError("Zoho did not return a refresh token (was access_type=offline sent?).")

    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=time.time() + int(payload.get("expires_in", 3600)),
        accounts_server=region.accounts_server,
        api_domain=_resolve_api_domain(payload, region.api_domain),
        scopes=list(scopes),
    )


async def revoke(http: httpx.AsyncClient, tokens: TokenSet) -> None:
    # Revoking the refresh token also invalidates every access token issued from it.
    await http.post(
        f"{tokens.accounts_server}/oauth/v2/token/revoke",
        data={"token": tokens.refresh_token.get_secret_value()},
    )


async def fetch_organizations(http: httpx.AsyncClient, tokens: TokenSet) -> list[dict]:
    response = await http.get(
        f"{tokens.region.inventory_base_url}/organizations",
        headers={"Authorization": f"Zoho-oauthtoken {tokens.access_token.get_secret_value()}"},
    )
    payload = response.json()
    if response.is_error or payload.get("code") != 0:
        raise OAuthError(f"Could not list organizations: {payload.get('message', response.status_code)}")
    return payload.get("organizations", [])


class TokenProvider:
    """Hands out a valid access token, refreshing it when it is about to expire or was rejected.

    Refreshes are serialised behind a lock: if five requests discover an expired token at
    once, only the first one calls Zoho and the rest reuse its result. Zoho throttles token
    generation, so a refresh stampede would itself cause failures.
    """

    REFRESH_MARGIN_SECONDS = 300

    def __init__(
        self,
        store: TokenStore,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        clock: Callable[[], float] = time.time,
    ):
        tokens = store.load()
        if tokens is None:
            raise NotAuthenticatedError("Not connected to Zoho. Run `zoho-mcp auth login` first.")
        self._store = store
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._tokens = tokens
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    async def access_token(self) -> str:
        current = self._tokens.access_token.get_secret_value()
        if self._tokens.seconds_until_expiry(self._clock()) > self.REFRESH_MARGIN_SECONDS:
            return current
        return await self._refresh(stale_token=current)

    async def handle_rejected(self, rejected_token: str) -> str:
        """Call when Zoho answered 401 for `rejected_token`; returns a token worth retrying with."""
        return await self._refresh(stale_token=rejected_token)

    async def _refresh(self, stale_token: str) -> str:
        async with self._lock:
            current = self._tokens.access_token.get_secret_value()
            if current != stale_token:
                return current  # another caller refreshed while we waited for the lock

            response = await self._http.post(
                f"{self._tokens.accounts_server}/oauth/v2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.refresh_token.get_secret_value(),
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            try:
                payload = _parse_token_response(response)
            except OAuthError as exc:
                raise NotAuthenticatedError(
                    f"{exc}. The Zoho connection may have been revoked - run `zoho-mcp auth login`."
                ) from None

            # model_copy skips validation, so wrap the secret ourselves.
            self._tokens = self._tokens.model_copy(
                update={
                    "access_token": SecretStr(payload["access_token"]),
                    "expires_at": self._clock() + int(payload.get("expires_in", 3600)),
                    "api_domain": _resolve_api_domain(payload, self._tokens.api_domain),
                }
            )
            self._store.save(self._tokens)
            return payload["access_token"]
