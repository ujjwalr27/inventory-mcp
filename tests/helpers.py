"""Shared test doubles. Fixtures in tests/fixtures/ are real Zoho responses for the
fictional seeded demo org, with the account owner's name/email scrubbed."""

import asyncio
import json
from pathlib import Path

import httpx

from zoho_inventory_mcp.auth.token_store import TokenSet

BASE = "https://www.zohoapis.in/inventory/v1"
ORG = "60001"
FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeTime:
    """Deterministic clock: sleeping advances time instantly."""

    def __init__(self, start: float = 1_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeTokens:
    def __init__(self):
        self.tokens = TokenSet(
            access_token="t1",
            refresh_token="r",
            expires_at=10**12,
            accounts_server="https://accounts.zoho.in",
            api_domain="https://www.zohoapis.in",
            scopes=["ZohoInventory.items.READ"],
            organization_id=ORG,
            organization_name="Demo Kurta Store",
        )
        self.current = "t1"
        self.refreshes = 0

    async def access_token(self) -> str:
        return self.current

    async def handle_rejected(self, rejected: str) -> str:
        self.refreshes += 1
        self.current = f"t{self.refreshes + 1}"
        return self.current


def ok(body: dict | None = None, **headers) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "message": "success", **(body or {})}, headers=headers)


def zoho_error(status: int, code: int, message: str = "error", **headers) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message}, headers=headers)
