"""Bearer-token gate for the HTTP transport, which makes this a *private* connector.

Only callers holding MCP_SERVER_TOKEN (e.g. the merchant's Agent Studio workspace) can
reach the tools. This is separate from the Zoho OAuth token, which never leaves the server.
"""

import json
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        if len(token) < 16:
            raise ValueError("MCP_SERVER_TOKEN must be at least 16 characters.")
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not secrets.compare_digest(supplied, self._expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="zoho-inventory-mcp"'),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
