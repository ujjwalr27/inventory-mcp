"""Persists OAuth tokens for one merchant org.

Tokens live outside the repo (default ~/.zoho-mcp/<profile>.json). Profiles keep the
connector's read-only token separate from the seed script's write token.
"""

import os
import time
from pathlib import Path

from pydantic import BaseModel, SecretStr, field_serializer

from zoho_inventory_mcp.regions import Region, region_for_api_domain


class TokenSet(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: float
    accounts_server: str
    api_domain: str
    scopes: list[str]
    organization_id: str | None = None
    organization_name: str | None = None

    # SecretStr keeps tokens out of repr/logs; only the on-disk JSON holds real values.
    @field_serializer("access_token", "refresh_token", when_used="json")
    def _reveal(self, value: SecretStr) -> str:
        return value.get_secret_value()

    @property
    def region(self) -> Region:
        return region_for_api_domain(self.api_domain)

    def seconds_until_expiry(self, now: float | None = None) -> float:
        return self.expires_at - (time.time() if now is None else now)


class TokenStore:
    def __init__(self, directory: Path, profile: str = "connector"):
        self.path = directory / f"{profile}.json"

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        return TokenSet.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(tokens.model_dump_json(indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        # Atomic swap so a crash mid-write never leaves a half-written token file.
        os.replace(tmp, self.path)

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False
