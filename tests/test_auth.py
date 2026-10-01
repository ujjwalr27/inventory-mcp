import asyncio
import socket
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from zoho_inventory_mcp.auth.oauth import (
    READ_SCOPES,
    CallbackResult,
    NotAuthenticatedError,
    OAuthError,
    TokenProvider,
    authorize_url,
    exchange_code,
    wait_for_callback,
)
from zoho_inventory_mcp.auth.token_store import TokenSet, TokenStore
from zoho_inventory_mcp.regions import UnknownRegionError, region_for_accounts_server, region_for_api_domain

IN_ACCOUNTS = "https://accounts.zoho.in"
IN_API = "https://www.zohoapis.in"


def make_tokens(**overrides) -> TokenSet:
    values = dict(
        access_token="old-access",
        refresh_token="refresh-1",
        expires_at=1_000_000.0,
        accounts_server=IN_ACCOUNTS,
        api_domain=IN_API,
        scopes=list(READ_SCOPES),
        organization_id="60001",
        organization_name="Demo Kurta Store",
    )
    values.update(overrides)
    return TokenSet(**values)


# --- regions -----------------------------------------------------------------


def test_region_lookup_normalises_case_and_trailing_slash():
    assert region_for_accounts_server("HTTPS://accounts.zoho.in/").code == "in"
    assert region_for_api_domain("https://www.zohoapis.com.au").code == "au"


def test_region_rejects_non_zoho_hosts():
    # The accounts-server value comes from a redirect query string; never trust arbitrary hosts.
    with pytest.raises(UnknownRegionError):
        region_for_accounts_server("https://accounts.zoho.in.evil.example")


# --- token store ---------------------------------------------------------------


def test_token_store_round_trip_and_masks_secrets_in_repr(tmp_path):
    store = TokenStore(tmp_path, "connector")
    store.save(make_tokens())
    loaded = store.load()
    assert loaded.access_token.get_secret_value() == "old-access"
    assert "old-access" not in repr(loaded)
    assert store.clear() and store.load() is None


# --- authorize URL / callback ----------------------------------------------------


def test_authorize_url_requests_offline_read_only_access():
    url = authorize_url(
        accounts_server=IN_ACCOUNTS,
        client_id="cid",
        redirect_uri="http://localhost:8765/callback",
        scopes=READ_SCOPES,
        state="xyz",
    )
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["state"] == ["xyz"]
    assert all(s.endswith(".READ") for s in query["scope"][0].split(","))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hit_callback_later(url: str) -> None:
    def go():
        for _ in range(50):
            try:
                httpx.get(url, timeout=2)
                return
            except httpx.ConnectError:
                threading.Event().wait(0.05)

    threading.Thread(target=go, daemon=True).start()


def test_callback_captures_code_and_region():
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    _hit_callback_later(f"{redirect}?code=abc&state=s1&location=in&accounts-server={IN_ACCOUNTS}")
    result = wait_for_callback(redirect, "s1", timeout=10)
    assert result == CallbackResult(code="abc", accounts_server=IN_ACCOUNTS, location="in")


def test_callback_rejects_state_mismatch():
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    _hit_callback_later(f"{redirect}?code=abc&state=forged")
    with pytest.raises(OAuthError, match="State mismatch"):
        wait_for_callback(redirect, "s1", timeout=10)


# --- code exchange ------------------------------------------------------------------


@respx.mock
async def test_exchange_uses_region_from_callback():
    route = respx.post("https://accounts.zoho.eu/oauth/v2/token").respond(
        json={"access_token": "a", "refresh_token": "r", "expires_in": 3600, "api_domain": "https://www.zohoapis.eu"}
    )
    async with httpx.AsyncClient() as http:
        tokens = await exchange_code(
            http,
            callback=CallbackResult("code", "https://accounts.zoho.eu", "eu"),
            default_accounts_server=IN_ACCOUNTS,
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://localhost:8765/callback",
            scopes=READ_SCOPES,
        )
    assert route.called
    assert tokens.region.code == "eu"


@respx.mock
async def test_exchange_surfaces_zoho_200_error_body():
    respx.post(f"{IN_ACCOUNTS}/oauth/v2/token").respond(json={"error": "invalid_code"})
    async with httpx.AsyncClient() as http:
        with pytest.raises(OAuthError, match="invalid_code"):
            await exchange_code(
                http,
                callback=CallbackResult("code", None, None),
                default_accounts_server=IN_ACCOUNTS,
                client_id="cid",
                client_secret="secret",
                redirect_uri="http://localhost:8765/callback",
                scopes=READ_SCOPES,
            )


# --- refresh ---------------------------------------------------------------------------


@respx.mock
async def test_valid_token_is_reused_without_refresh(tmp_path):
    store = TokenStore(tmp_path)
    store.save(make_tokens(expires_at=10_000))
    route = respx.post(f"{IN_ACCOUNTS}/oauth/v2/token")
    async with httpx.AsyncClient() as http:
        provider = TokenProvider(store, http, client_id="c", client_secret="s", clock=lambda: 0)
        assert await provider.access_token() == "old-access"
    assert not route.called


@respx.mock
async def test_concurrent_expiry_triggers_exactly_one_refresh(tmp_path):
    store = TokenStore(tmp_path)
    store.save(make_tokens(expires_at=100))  # inside the 5-minute refresh margin
    route = respx.post(f"{IN_ACCOUNTS}/oauth/v2/token").respond(
        json={"access_token": "new-access", "expires_in": 3600, "api_domain": IN_API}
    )
    async with httpx.AsyncClient() as http:
        provider = TokenProvider(store, http, client_id="c", client_secret="s", clock=lambda: 0)
        results = await asyncio.gather(*(provider.access_token() for _ in range(10)))

    assert results == ["new-access"] * 10
    assert route.call_count == 1
    assert store.load().access_token.get_secret_value() == "new-access"
    assert store.load().refresh_token.get_secret_value() == "refresh-1"  # Zoho does not rotate it


@respx.mock
async def test_revoked_refresh_token_asks_user_to_log_in_again(tmp_path):
    store = TokenStore(tmp_path)
    store.save(make_tokens(expires_at=0))
    respx.post(f"{IN_ACCOUNTS}/oauth/v2/token").respond(json={"error": "invalid_code"})
    async with httpx.AsyncClient() as http:
        provider = TokenProvider(store, http, client_id="c", client_secret="s", clock=lambda: 0)
        with pytest.raises(NotAuthenticatedError, match="auth login"):
            await provider.access_token()


def test_provider_requires_login(tmp_path):
    with pytest.raises(NotAuthenticatedError):
        TokenProvider(TokenStore(tmp_path), httpx.AsyncClient(), client_id="c", client_secret="s")
