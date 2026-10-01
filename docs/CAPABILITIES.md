# What the agent can and cannot do

This connector gives an Agent Studio agent **read-only** access to one merchant's Zoho Inventory organization.

## ✅ The agent can

**Products and stock**
- Find products by name or SKU, and see selling price, cost price and unit.
- See stock in three ways: **on hand** (physical), **available for sale** (on hand minus units committed to open orders) and **committed**.
- List everything at or below its reorder level ("what should I restock?"), using Zoho's own low-stock view.
- Tell whether a product is out of stock.

**Sales orders**
- Find orders by Zoho order number (`SO-00001`), the merchant's own reference, customer name or ID, status (draft / confirmed / shipped) or date range.
- See what an order contains: items, quantities, prices, total and currency.
- Follow fulfillment: each order has a `fulfillment_stage` (draft, confirmed, partially packed, packed, partially shipped, shipped, delivered, void).
- For shipped orders, see the package, shipment number, carrier, tracking number and ship date.
- See the destination city and state.

**Customers**
- Find customers by name, email or phone (any phone format, at least 5 digits).
- See a customer's name, company, email, phone and city/state, and link them to their orders.

**The connection itself**
- Report which organization and region are connected, and how much of today's Zoho API quota is left.

## ❌ The agent cannot

| Cannot | Why |
|---|---|
| Create, edit, confirm, cancel or delete anything (orders, items, customers, stock) | Read-only by design: the OAuth token has READ scopes only, and every tool is marked `readOnlyHint`. Changes must be made in Zoho Inventory. |
| See invoices, payments, refunds, outstanding balances or payment status | Out of scope; payment fields are stripped even where Zoho includes them in order or contact records. |
| See purchase orders, bills, vendors' details, composite items or per-warehouse stock | Not part of this connector's three resources. |
| See product descriptions, order notes or other free text | Dropped deliberately: rarely needed, and anyone who can edit a record controls that text (a prompt-injection risk). |
| See full street addresses | Reduced to city and state. |
| Answer analytics ("revenue this month", "best seller") directly | No aggregation endpoints. The agent can only reason over the orders it lists, a page at a time (max 50 per call). |
| Filter orders by "packed" in Zoho | Zoho has no such filter. The agent searches "confirmed" orders and reads `fulfillment_stage`. |
| Access more than one Zoho organization | One org per connector, chosen at login. |
| Guarantee second-by-second freshness | Results may be cached for up to 30 s (searches) or 60 s (details). `fresh=true` bypasses the cache when the user asks for live data. |

## How the agent should handle errors

Tool errors come back as JSON with `type`, `retryable`, `retry_after_seconds`, a `message` written for the agent, and a technical `detail`.

| `type` | Meaning | What the agent should do |
|---|---|---|
| `invalid_request` | Bad arguments (e.g. date not `YYYY-MM-DD`, phone with too few digits) | Fix the arguments using `detail` and call again |
| `not_found` / `found: false` | No record with that ID or number | Search instead of guessing IDs |
| `rate_limited` | Per-minute limit; the connector is pacing calls | Wait `retry_after_seconds`, or tell the user to try again shortly |
| `concurrency_limited` | Too many simultaneous calls (rare; retried internally first) | Retry after a few seconds |
| `daily_limit_exceeded` | The merchant's daily Zoho API quota is used up | **Do not retry.** Tell the user data is unavailable until the quota resets |
| `upstream_unavailable` | Zoho is down or timing out (already retried 3 times) | Tell the user Zoho is temporarily unavailable |
| `permission_denied` / `not_connected` | Access was revoked, or Zoho was never connected | Tell the user the merchant must reconnect Zoho Inventory |

A `warnings` list on any result (e.g. "daily quota nearly used up") should be passed on to the user.

## Example conversations (demo org)

| User asks | Tools used | Result |
|---|---|---|
| "What should I restock?" | `list_low_stock_items` | 5 items ranked by urgency; it pointed out the maroon saree has 4 on hand but only 3 available for sale |
| "Has Priya Sharma's order shipped?" | `search_sales_orders` → `get_sales_order` | SO-00001 shipped via Delhivery, tracking DEMO-TRK-01, to Jaipur |
| "Who is +91 55500 01003?" | `search_contacts` → `get_contact` | Ananya Iyer, Chennai |
| "Is the blue kurta in stock in L?" | `search_items` | 6 available, below its reorder level of 10 |
| "Cancel order SO-00012" | none | Expected: declines, because the server instructions state the connector is read-only and changes must be made in Zoho Inventory |
