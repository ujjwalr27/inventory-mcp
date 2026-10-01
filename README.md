# Zoho Inventory Connector for Agent Studio (MCP)

A **private, read-only connector** that lets an Agent Studio agent (or any MCP client) look up a merchant's **products and stock, sales orders and shipping status, and customers** in Zoho Inventory.

- **Auth:** OAuth 2.0 authorization-code flow with automatic token refresh and Zoho region auto-detection.
- **Read-only by design:** requests only READ scopes, and every tool is annotated `readOnlyHint`.
- **8 MCP tools:** list / get / search primitives, returning trimmed, agent-friendly data.
- **Rate-limit aware:** handles Zoho's three different 429s (per-minute, daily, concurrency) differently instead of blindly retrying.
- **Transports:** stdio for desktop clients; streamable HTTP behind a bearer token for remote agents.
- **Agent Studio compatible:** Agent Studio is built on the Claude Agent SDK, which consumes MCP servers natively. See [Using with Razorpay Agent Studio](#using-with-razorpay-agent-studio).

See [docs/CAPABILITIES.md](docs/CAPABILITIES.md) for what the agent can and cannot do, and [docs/tool-spec.json](docs/tool-spec.json) for the full MCP tool specification.

---

## Quickstart (about 5 minutes)

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), and a Zoho Inventory organization (the free plan works).

### 1. Create a Zoho OAuth client
At [api-console.zoho.in](https://api-console.zoho.in) (or your region's console), create a **Server-based Application**:

| Field | Value |
|---|---|
| Client name | anything without the word "Zoho" (Zoho rejects it), e.g. `Agent Studio Inventory Connector` |
| Homepage URL | `http://localhost:8765` |
| Authorized redirect URI | `http://localhost:8765/callback` |

Then go to **Settings → Multi-DC** and turn on "Use the same OAuth credentials for all data centers".

### 2. Configure
```bash
cp .env.example .env    # then fill in ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET
uv sync
```

### 3. Connect a Zoho organization
```bash
uv run zoho-mcp auth login            # opens the browser; approve read-only access
uv run zoho-mcp auth status --check   # verifies with one live API call
```
Tokens are stored in `~/.zoho-mcp/connector.json`, outside the repo.

### 4. (Optional) Load demo data
Run this to fill an empty org with fictional data:
```bash
uv run zoho-mcp auth login --profile seed      # separate token with write scopes, used only here
uv run python scripts/seed_demo_data.py         # add --dry-run to preview
```
It creates 20 items (5 below reorder level), 10 customers and 15 sales orders in every stage (draft, confirmed, packed, shipped). All records are tagged `DEMO-`, and the script is idempotent and resumable.

### 5. Connect an agent

**Claude Desktop / any stdio MCP client.** Add this to `claude_desktop_config.json`, alongside any existing keys:
```json
{
  "mcpServers": {
    "zoho-inventory": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/this/repo", "run", "zoho-mcp", "serve"]
    }
  }
}
```
On Windows, use the full path to `uv.exe` (e.g. `C:\\Users\\<you>\\.local\\bin\\uv.exe`), because desktop apps may not inherit your shell's PATH.

**Agent Studio / remote agents (HTTP).**
1. Set `MCP_SERVER_TOKEN` in `.env`:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. Start the server:
   ```bash
   uv run zoho-mcp serve --transport http --port 8000
   ```
3. The endpoint is `http://127.0.0.1:8000/mcp`. Clients must send `Authorization: Bearer <MCP_SERVER_TOKEN>`.
4. To reach it from a hosted agent, put it behind a tunnel (e.g. `cloudflared tunnel --url http://localhost:8000`) and allow the tunnel hostname:
   ```bash
   uv run zoho-mcp serve --transport http --allowed-host <tunnel-host>
   ```

### 6. Verify end to end
```bash
uv run python scripts/smoke_test.py   # starts the server over stdio and calls every tool (~15 Zoho calls)
uv run pytest -q                      # 80 offline tests
```

---

## Using with Razorpay Agent Studio

Agent Studio is built on Anthropic's **Claude Agent SDK** ([Razorpay](https://razorpay.com/blog/agent-studio-ai-agents-by-razorpay/)). That SDK is the *agent* side and connects to tools through **MCP servers**. This connector is an MCP server, built on the official **MCP Python SDK** (the *tool* side), so an Agent Studio agent can use it without any adapter.

| Agent Studio expects | This connector |
|---|---|
| Tools exposed over MCP | 8 MCP tools with JSON schemas ([docs/tool-spec.json](docs/tool-spec.json)) |
| A remote endpoint over streamable HTTP, the same pattern as Razorpay's own [remote MCP server](https://razorpay.com/docs/mcp-server/remote/) at `/mcp` | `zoho-mcp serve --transport http`, serving `/mcp` |
| Private access per merchant | Bearer token (`MCP_SERVER_TOKEN`); Zoho OAuth tokens never leave the server |
| Safe-by-default tools | Every tool annotated `readOnlyHint: true`, `destructiveHint: false`; server instructions state the read-only boundary |
| Agent-actionable failures | Structured errors with `type`, `retryable` and `retry_after_seconds` ([docs/CAPABILITIES.md](docs/CAPABILITIES.md)) |

An agent built on the Claude Agent SDK attaches the connector like this (Python; format from the [Agent SDK MCP docs](https://code.claude.com/docs/en/agent-sdk/mcp#http-headers-for-remote-servers)):

```python
from claude_agent_sdk import ClaudeAgentOptions

options = ClaudeAgentOptions(
    mcp_servers={
        "zoho-inventory": {
            "type": "http",
            "url": "https://<your-tunnel-or-host>/mcp",
            "headers": {"Authorization": "Bearer <MCP_SERVER_TOKEN>"},
        }
    },
    allowed_tools=["mcp__zoho-inventory__*"],
)
```

**What has been verified:**
- The tools were called through a real MCP client (Claude Desktop) against a live Zoho org.
- The HTTP transport was checked live: requests without the token get 401, and tool calls succeed with it.

**What hasn't:** running inside Agent Studio itself. Agent Studio has no public way to register a custom connector, so the integration path above is based on its published architecture (the Claude Agent SDK) and on Razorpay's own MCP server pattern.

---

## Commands

| Command | Purpose |
|---|---|
| `zoho-mcp auth login [--profile connector\|seed] [--org-id ID] [--no-browser]` | Authorize in the browser and store tokens |
| `zoho-mcp auth status [--check]` | Show org, region, scopes and token expiry; `--check` makes one live call |
| `zoho-mcp auth logout` | Revoke the refresh token at Zoho and delete local tokens |
| `zoho-mcp serve [--transport stdio\|http] [--host] [--port] [--allowed-host]` | Run the MCP server |
| `zoho-mcp export-spec [--output docs/tool-spec.json]` | Write the MCP tool specification (needs no credentials) |

---

## Tools

| Tool | Answers questions like | Zoho calls |
|---|---|---|
| `search_items` | "Do we have the blue kurta? How many left? Price?" | 1 |
| `get_item` | "How much of SKU X is committed to orders?" | 1 (2 when looked up by SKU) |
| `list_low_stock_items` | "What should I restock?" | 1 (Zoho's native low-stock view) |
| `search_sales_orders` | "Priya's orders", "drafts from last week", "order SO-00004" | 1–2 |
| `get_sales_order` | "What did they buy? Has it shipped? Tracking number?" | 1 (2 when looked up by number) |
| `search_contacts` | "Who is the customer with phone 98…?" | 1 |
| `get_contact` | Customer details | 1 |
| `get_connector_status` | "Is the inventory connection working? How much quota is left?" | 0 |

**Results**
- List results share one envelope: `results`, `count`, `next_cursor`, `organization`, `cached`, `fetched_at`, `warnings`.
- `get_*` tools return `found: true|false` rather than raising on unknown IDs.

**Errors** come back as structured MCP errors the agent can act on:
```json
{"error": {"type": "daily_limit_exceeded", "retryable": false, "retry_after_seconds": 14487,
           "message": "This merchant's daily Zoho API quota is used up. Tell the user ...", "detail": "..."}}
```

---

## How it works

```
MCP tool ─► resources/ (params → Zoho query, raw → trimmed model)
         ─► ZohoClient: cache + single-flight → daily budget → per-minute bucket → concurrency gate
                        → OAuth header + organization_id → HTTP → classify response → retry or raise
```

### Authentication
- **Login:** a one-shot local callback server validates `state` (CSRF) and receives the code plus Zoho's `accounts-server`. The code is exchanged at **that** data center, so merchants on `.in`, `.com`, `.eu`, etc. all work.
- **Region allowlist:** region hosts are checked against an allowlist before the client secret or tokens are sent anywhere.
- **Organization:** `organization_id` is fetched once after login (with a prompt if the account has several orgs) and added to every request.
- **Token refresh:** access tokens are refreshed 5 minutes before expiry, and again if Zoho rejects one. Refreshes sit behind a lock, so 10 concurrent expiries cause **1** refresh. If the refresh token is revoked, the agent gets a clear "reconnect" error.
- **Two token profiles:** `connector` holds READ scopes only. `seed` adds the write scopes the demo-data script needs. The connector can't write even if a bug tried to.

### Rate-limit handling
Zoho returns HTTP 429 for three different limits and tells them apart only by the body `code`. The connector paces itself so it rarely sees them, and reads the code when it does.

| Limit (Zoho) | Prevention | If Zoho still returns it |
|---|---|---|
| **100 req/min/org** (code `44`) | Token bucket at 90/min, leaving headroom for the merchant's other integrations on the same quota. **Reservation-based:** a call learns its wait up front, and if that's over 20 s the agent is told "retry in N s" immediately instead of hanging. | Freeze new calls for `Retry-After` (default 60 s); retry at most twice, and only if the wait is short. Hammering past this limit can get an org blocked. |
| **Daily quota** (code `45`; Free 1000/day, higher on paid plans) | Tracks Zoho's `x-rate-limit-limit/-remaining/-reset` headers and adds a `warnings` entry to tool results when under 10% remains | **Never retried.** Later calls are refused locally until the reset time, without spending more requests, and the agent is told when data will be back. |
| **Concurrent calls** (code `1070`; 5 on Free, 10 on paid) | Semaphore sized by `ZOHO_PLAN` | Jittered exponential backoff (0.5 s, 1 s, 2 s), max 3 retries |
| 5xx / network errors | — | GETs retried at 1/2/4 s with jitter; writes (seed script only) are never retried |
| 401 | Proactive refresh | Refresh once and retry; a second 401 gives `permission_denied` ("merchant must reconnect") |

**Caching:** a 30 s (search) / 60 s (detail) TTL cache with **single-flight**, so N identical concurrent calls cost one Zoho request. Errors are never cached. Agents can pass `fresh=true` when the user explicitly asks for live data.

All of this is covered by offline tests with a fake clock in [tests/test_client.py](tests/test_client.py):
- the bucket never exceeds its rate
- 20 parallel calls peak at exactly 5 in flight
- a daily 429 leads to zero retries, and later calls never reach Zoho
- 10 identical calls send 1 request
- and more

### Agent-friendly data
Raw Zoho records are large: 117 keys per item, 129 per contact, about 170 per sales order. Each tool returns a trimmed model of roughly 10–15 clearly named fields:
- **Items:** `stock_on_hand` vs `available_for_sale` (minus stock committed to orders), plus `is_low_stock`.
- **Orders:** Zoho has no "packed" status (a packed order is still "confirmed"), so the connector derives `fulfillment_stage`: draft → confirmed → packed → shipped. Shipments come with carrier and tracking number.
- **Addresses:** reduced to city and state.
- **Excluded on purpose:** payment fields (`paid_status`, `balance`, receivables) and free-text fields (descriptions, notes). Free text is a prompt-injection vector, because anyone who can edit a record controls it.

---

## Project layout

```
src/zoho_inventory_mcp/
  cli.py              zoho-mcp commands
  config.py           settings from .env (ZOHO_ prefix)
  regions.py          Zoho data-center allowlist
  auth/oauth.py       login flow, code exchange, TokenProvider (refresh), revoke
  auth/token_store.py token file per profile, atomic writes, secrets masked in logs
  client/http.py      ZohoClient: the single request pipeline, pagination, stats
  client/ratelimit.py MinuteBucket (per-minute), DailyBudget
  client/errors.py    Zoho response → typed error with retry policy and agent message
  client/cache.py     TTL cache + single-flight
  resources/          items, sales_orders, contacts: tool params → Zoho params → models
  models.py           trimmed output models
  server.py           MCP tools (MCP Python SDK 2.x)
  http_auth.py        bearer-token gate for the HTTP transport
scripts/
  seed_demo_data.py   fictional demo data (separate write token)
  smoke_test.py       live end-to-end check over stdio
tests/                80 tests; fixtures are real responses from the fictional demo org, owner details scrubbed
docs/                 CAPABILITIES.md, tool-spec.json
```

---

## Assumptions

- **One merchant organization per running server.** The account owner picks the org at login.
- **Plan:** developed against a Zoho Inventory free/trial org on the **India (.in)** data center. The other data centers are supported by design but not tested live.
- **Read-only** is a deliberate scope choice, not a gap (see "Path to writes" below).
- **Rate-limit tuning:** the plan in `ZOHO_PLAN` (default `free`) sets the concurrency limit and the daily-quota fallback, until Zoho's own quota headers are seen.

## Limitations and long-term fixes

| Limitation | Why | Long-term fix |
|---|---|---|
| Single tenant: one org, tokens in a local file | Fits a per-merchant deployment and keeps the demo simple | Multi-tenant OAuth: per-merchant tokens in an encrypted vault (KMS), keyed by Agent Studio workspace |
| HTTP auth is one shared bearer token | Simple and sufficient for a private connector | MCP's OAuth authorization spec, or Agent Studio-issued short-lived tokens with per-workspace scoping |
| Data is fetched live (with up to 60 s caching) | No sync infrastructure needed | Zoho webhooks feeding a local mirror; far fewer API calls and instant reads |
| Rate-limit state is per process | One server per merchant | Shared limiter (e.g. Redis) when several replicas serve one org |
| Search is limited to what Zoho's list filters support (no "packed" filter, no aggregates like "revenue this month") | The connector only exposes verified Zoho filters | Reporting endpoints or the local mirror above |
| No warehouse-level stock, composite items, invoices, payments, purchase orders | Kept to the three resources the agent needs most | Add resources behind their own READ scopes |
| Phone search sends the last 5 digits to Zoho and matches the rest locally | Zoho's `phone_contains` is a plain substring match on formatted text | Store normalised phone numbers (E.164) in the mirror |
| Demo data has no teardown | Deleting shipped orders needs package/shipment DELETE scopes | Use a fresh org, or delete `DEMO-` records in the Zoho UI |

### Path to writes (not built on purpose)
Writes change the merchant's books, so they need more safeguards than reads. The proposed design:
- **Propose, then confirm:** `propose_*` returns a diff and a one-time confirmation token; `confirm_*` executes it only with that token, after the human approves.
- **Duplicate protection:** an idempotency key on every write, so an LLM retry can't confirm or adjust twice.
- **Opt-in permissions:** a separate OAuth profile with only the needed scopes (the `seed` profile already shows the pattern).
- **Accountability:** an audit log, and per-merchant write limits.
- **Order of rollout:** start with low-risk, easy-to-undo actions (confirm a draft order, add a note) before stock adjustments or cancellations.

---

## Security notes
- No credentials in the repo: `.env` and token files are git-ignored, tokens live in `~/.zoho-mcp/`, and `SecretStr` keeps them out of logs and reprs.
- The OAuth `state` is verified on the callback, and only allowlisted Zoho hosts ever receive the client secret.
- The HTTP transport compares the bearer token in constant time (minimum 16 characters) and has DNS-rebinding protection on.
- All data in this repo (fixtures, seed script) is fictional. Emails use `example.com`, and phone numbers start with `+91 555`, which is not a valid Indian mobile prefix.
