from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Plan = Literal["free", "standard", "professional", "premium", "enterprise"]


class Settings(BaseSettings):
    """Connector configuration, read from environment variables / .env (prefix ZOHO_)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="ZOHO_", extra="ignore")

    client_id: str
    client_secret: SecretStr
    redirect_uri: str = "http://localhost:8765/callback"
    default_accounts_server: str = "https://accounts.zoho.in"
    plan: Plan = "free"
    token_dir: Path = Path.home() / ".zoho-mcp"
    # Shared secret clients must send as `Authorization: Bearer ...` to the HTTP transport.
    mcp_server_token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MCP_SERVER_TOKEN", "ZOHO_MCP_SERVER_TOKEN")
    )

    @field_validator("token_dir", mode="before")
    @classmethod
    def _expand_home(cls, value: str | Path) -> Path:
        return Path(value).expanduser()
