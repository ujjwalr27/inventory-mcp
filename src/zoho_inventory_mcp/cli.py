"""Command-line entry point: `zoho-mcp ...`."""

import asyncio
import logging
import secrets
import time
import webbrowser

import httpx
import typer
from pydantic import ValidationError

from zoho_inventory_mcp.auth.oauth import (
    READ_SCOPES,
    SEED_SCOPES,
    OAuthError,
    TokenProvider,
    authorize_url,
    exchange_code,
    fetch_organizations,
    revoke,
    wait_for_callback,
)
from zoho_inventory_mcp.auth.token_store import TokenSet, TokenStore
from zoho_inventory_mcp.config import Settings

app = typer.Typer(no_args_is_help=True, help="Read-only Zoho Inventory connector for Agent Studio (MCP).")
auth_app = typer.Typer(no_args_is_help=True, help="Connect to or disconnect from a Zoho Inventory organization.")
app.add_typer(auth_app, name="auth")

PROFILE_HELP = "Token profile. 'connector' is read-only; 'seed' adds write scopes for the demo-data script."


def _settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = ", ".join(f"ZOHO_{'_'.join(map(str, e['loc'])).upper()}" for e in exc.errors())
        typer.secho(f"Missing or invalid configuration: {missing}. See .env.example.", fg="red")
        raise typer.Exit(1) from None


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg="red")
    return typer.Exit(1)


def _choose_org(orgs: list[dict], org_id: str | None) -> dict:
    if not orgs:
        raise _fail("This Zoho account has no Inventory organizations. Create one in Zoho Inventory first.")
    if org_id:
        match = next((o for o in orgs if str(o["organization_id"]) == org_id), None)
        if match is None:
            raise _fail(f"Organization {org_id} not found for this account.")
        return match
    if len(orgs) == 1:
        return orgs[0]
    for i, org in enumerate(orgs, 1):
        typer.echo(f"  {i}. {org['name']} ({org['organization_id']})")
    choice = typer.prompt("Which organization should the agent read from?", type=int)
    if not 1 <= choice <= len(orgs):
        raise _fail("Invalid choice.")
    return orgs[choice - 1]


@auth_app.command("login")
def login(
    profile: str = typer.Option("connector", help=PROFILE_HELP),
    org_id: str | None = typer.Option(None, help="Organization ID to use if the account has several."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the URL instead of opening a browser."),
) -> None:
    """Authorize with Zoho in the browser and store tokens locally."""
    settings = _settings()
    if profile not in ("connector", "seed"):
        raise _fail("Profile must be 'connector' or 'seed'.")
    scopes = READ_SCOPES if profile == "connector" else SEED_SCOPES

    state = secrets.token_urlsafe(24)
    url = authorize_url(
        accounts_server=settings.default_accounts_server,
        client_id=settings.client_id,
        redirect_uri=settings.redirect_uri,
        scopes=scopes,
        state=state,
    )
    typer.echo(f"Requesting scopes: {', '.join(scopes)}")
    if no_browser or not webbrowser.open(url):
        typer.echo(f"Open this URL to authorize:\n\n{url}\n")
    else:
        typer.echo("Opened your browser - approve access there. Waiting for Zoho to redirect back...")

    try:
        callback = wait_for_callback(settings.redirect_uri, state)
    except OAuthError as exc:
        raise _fail(str(exc)) from None
    except OSError as exc:
        raise _fail(f"Could not listen on {settings.redirect_uri}: {exc}") from None

    async def finish() -> TokenSet:
        async with httpx.AsyncClient(timeout=20) as http:
            tokens = await exchange_code(
                http,
                callback=callback,
                default_accounts_server=settings.default_accounts_server,
                client_id=settings.client_id,
                client_secret=settings.client_secret.get_secret_value(),
                redirect_uri=settings.redirect_uri,
                scopes=scopes,
            )
            orgs = await fetch_organizations(http, tokens)
        org = _choose_org(orgs, org_id)
        return tokens.model_copy(
            update={"organization_id": str(org["organization_id"]), "organization_name": org["name"]}
        )

    try:
        tokens = asyncio.run(finish())
    except OAuthError as exc:
        raise _fail(str(exc)) from None

    TokenStore(settings.token_dir, profile).save(tokens)
    typer.secho(
        f"Connected to '{tokens.organization_name}' ({tokens.organization_id}) "
        f"in region '{tokens.region.code}'. Profile '{profile}' saved.",
        fg="green",
    )


@auth_app.command("status")
def status(
    profile: str = typer.Option("connector", help=PROFILE_HELP),
    check: bool = typer.Option(False, "--check", help="Also make one live API call (refreshing the token if needed)."),
) -> None:
    """Show which org this connector is connected to."""
    settings = _settings()
    store = TokenStore(settings.token_dir, profile)
    tokens = store.load()
    if tokens is None:
        raise _fail(f"Not connected (profile '{profile}'). Run `zoho-mcp auth login`.")

    remaining = tokens.seconds_until_expiry()
    typer.echo(f"Organization : {tokens.organization_name} ({tokens.organization_id})")
    typer.echo(f"Region       : {tokens.region.code}  ({tokens.api_domain})")
    typer.echo(f"Scopes       : {', '.join(tokens.scopes)}")
    typer.echo(
        f"Access token : {'valid for ' + str(int(remaining // 60)) + ' min' if remaining > 0 else 'expired'}"
        " (refreshes automatically)"
    )
    typer.echo(f"Token file   : {store.path}")

    if not check:
        return

    async def live_check() -> int:
        async with httpx.AsyncClient(timeout=20) as http:
            provider = TokenProvider(
                store,
                http,
                client_id=settings.client_id,
                client_secret=settings.client_secret.get_secret_value(),
            )
            await provider.access_token()
            started = time.perf_counter()
            orgs = await fetch_organizations(http, provider.tokens)
            typer.echo(f"Live check   : OK ({len(orgs)} org(s), {(time.perf_counter() - started) * 1000:.0f} ms)")
            return len(orgs)

    try:
        asyncio.run(live_check())
    except OAuthError as exc:
        raise _fail(f"Live check failed: {exc}") from None


@auth_app.command("logout")
def logout(profile: str = typer.Option("connector", help=PROFILE_HELP)) -> None:
    """Revoke the refresh token at Zoho and delete local tokens."""
    settings = _settings()
    store = TokenStore(settings.token_dir, profile)
    tokens = store.load()
    if tokens is None:
        typer.echo("Already logged out.")
        return

    async def do_revoke() -> None:
        async with httpx.AsyncClient(timeout=20) as http:
            await revoke(http, tokens)

    try:
        asyncio.run(do_revoke())
    except httpx.HTTPError as exc:
        typer.secho(f"Could not reach Zoho to revoke ({exc}); deleting local tokens anyway.", fg="yellow")
    store.clear()
    typer.secho(f"Logged out of '{tokens.organization_name}'.", fg="green")


@app.command("serve")
def serve(
    transport: str = typer.Option("stdio", help="'stdio' for desktop MCP clients, 'http' for remote agents."),
    host: str = typer.Option("127.0.0.1", help="HTTP bind address."),
    port: int = typer.Option(8000, help="HTTP port."),
    allowed_host: list[str] = typer.Option(
        [], help="Extra Host header to accept over HTTP, e.g. your tunnel hostname. Repeatable."
    ),
) -> None:
    """Run the MCP server."""
    from zoho_inventory_mcp.server import ConnectorRuntime, create_server

    settings = _settings()
    # httpx logs every request URL at INFO; keep the server's stderr for real problems.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    runtime = ConnectorRuntime(settings)
    server = create_server(runtime.get_client)

    if transport == "stdio":
        # stdout carries the MCP protocol, so never print to it here.
        server.run("stdio")
        return
    if transport != "http":
        raise _fail("transport must be 'stdio' or 'http'.")
    if settings.mcp_server_token is None:
        raise _fail("Set MCP_SERVER_TOKEN in .env: the HTTP transport refuses to run without a bearer token.")

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from zoho_inventory_mcp.http_auth import BearerTokenMiddleware

    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"{host}:{port}", *allowed_host]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in allowed_host],
    )
    asgi_app = BearerTokenMiddleware(
        server.streamable_http_app(transport_security=security, host=host),
        settings.mcp_server_token.get_secret_value(),
    )
    typer.echo(f"MCP endpoint: http://{host}:{port}/mcp  (Authorization: Bearer <MCP_SERVER_TOKEN>)", err=True)
    uvicorn.run(asgi_app, host=host, port=port, log_level="warning")


@app.command("export-spec")
def export_spec(
    output: str = typer.Option("docs/tool-spec.json", help="Where to write the MCP tool specification."),
) -> None:
    """Write the MCP tool list (names, descriptions, JSON schemas) to a file. Needs no credentials."""
    import json
    from pathlib import Path

    from zoho_inventory_mcp.server import INSTRUCTIONS, create_server

    async def unused_client():  # export only lists tools; it never calls them
        raise RuntimeError("not available during export")

    tools = asyncio.run(create_server(unused_client).list_tools())
    spec = {
        "server": "zoho-inventory",
        "instructions": INSTRUCTIONS,
        "tools": [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools],
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    typer.secho(f"Wrote {len(tools)} tools to {path}", fg="green")


if __name__ == "__main__":
    app()
