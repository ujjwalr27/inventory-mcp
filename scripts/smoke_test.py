"""Live end-to-end check: starts the connector over stdio (as Claude Desktop / Agent Studio
would), calls every tool against the connected Zoho org and prints compact results.

    uv run python scripts/smoke_test.py

Uses roughly 15 Zoho API calls. Requires `zoho-mcp auth login` and the seeded demo data.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CALLS = [
    ("get_connector_status", {}),
    ("search_items", {"query": "kurta"}),
    ("get_item", {"sku": "DEMO-KUR-BLU-M"}),
    ("list_low_stock_items", {}),
    ("search_contacts", {"phone": "5550001001"}),
    ("search_sales_orders", {"customer_name": "Priya Sharma"}),
    ("get_sales_order", {"order_number": "DEMO-SO-01"}),
    ("search_sales_orders", {"status": "draft", "limit": 2}),
    ("search_sales_orders", {"date_from": "2026-13-01"}),  # expected: invalid_request
    ("get_item", {"item_id": "1"}),  # expected: found=false
    ("search_items", {"query": "kurta"}),  # expected: served from cache
    ("get_connector_status", {}),
]


def summarise(name: str, data: dict) -> str:
    if "results" in data:
        keys = {
            "search_items": ("sku", "available_for_sale", "is_low_stock"),
            "list_low_stock_items": ("sku", "available_for_sale", "reorder_level"),
            "search_contacts": ("name", "phone"),
            "search_sales_orders": ("order_number", "reference_number", "customer_name", "fulfillment_stage"),
        }[name]
        rows = [tuple(r[k] for k in keys) for r in data["results"]]
        return f"{data['count']} result(s), cached={data['cached']}: {rows}"
    if "found" in data:
        record = next((v for k, v in data.items() if isinstance(v, dict)), None)
        if not data["found"]:
            return f"found=false ({data['hint']})"
        if "shipments" in record:
            return (
                f"{record['order_number']} {record['fulfillment_stage']}, lines="
                f"{[(li['sku'], li['quantity']) for li in record['line_items']]}, shipments="
                f"{[(s['carrier'], s['tracking_number']) for s in record['shipments']]}"
            )
        return json.dumps(record)
    return json.dumps({k: data[k] for k in ("organization", "region", "daily_quota", "session") if k in data})


async def main() -> int:
    params = StdioServerParameters(command="uv", args=["run", "zoho-mcp", "serve", "--transport", "stdio"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print(f"Connected. {len(tools.tools)} tools: {', '.join(t.name for t in tools.tools)}\n")
        for name, args in CALLS:
            result = await session.call_tool(name, args)
            label = f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
            if result.is_error:
                error = json.loads(result.content[0].text[result.content[0].text.index("{"):])["error"]
                print(f"- {label}\n    ERROR {error['type']}: {error['detail']}")
            else:
                print(f"- {label}\n    {summarise(name, result.structured_content)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
