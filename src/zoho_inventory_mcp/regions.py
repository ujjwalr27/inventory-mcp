"""Zoho data centers.

Zoho tells us which data center an account lives in (via the OAuth callback and the
token response). We trust that answer only if it names a real Zoho host, because we
are about to send our client secret and the user's tokens to it.
"""

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Region:
    code: str
    accounts_server: str
    api_domain: str

    @property
    def inventory_base_url(self) -> str:
        return f"{self.api_domain}/inventory/v1"


REGIONS: tuple[Region, ...] = (
    Region("us", "https://accounts.zoho.com", "https://www.zohoapis.com"),
    Region("eu", "https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    Region("in", "https://accounts.zoho.in", "https://www.zohoapis.in"),
    Region("au", "https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    Region("jp", "https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    Region("ca", "https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    Region("cn", "https://accounts.zoho.com.cn", "https://www.zohoapis.com.cn"),
    Region("sa", "https://accounts.zoho.sa", "https://www.zohoapis.sa"),
)

_BY_ACCOUNTS_SERVER = {r.accounts_server: r for r in REGIONS}
_BY_API_DOMAIN = {r.api_domain: r for r in REGIONS}


class UnknownRegionError(ValueError):
    pass


def _origin(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def region_for_accounts_server(url: str) -> Region:
    try:
        return _BY_ACCOUNTS_SERVER[_origin(url)]
    except KeyError:
        raise UnknownRegionError(f"{url!r} is not a known Zoho accounts server") from None


def region_for_api_domain(url: str) -> Region:
    try:
        return _BY_API_DOMAIN[_origin(url)]
    except KeyError:
        raise UnknownRegionError(f"{url!r} is not a known Zoho API domain") from None
