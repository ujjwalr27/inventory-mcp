"""Fill a Zoho Inventory org with fictional demo data.

    uv run zoho-mcp auth login --profile seed     # one-time, grants write scopes
    uv run python scripts/seed_demo_data.py [--dry-run]

Uses the separate `seed` token profile, so the connector's own token stays read-only.
Every record is tagged with a DEMO- SKU / reference, and anything that already exists
is skipped, so the script is safe to re-run. All people, emails and phone numbers are
fictional (Indian mobile numbers never start with 5).
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from zoho_inventory_mcp.auth.oauth import TokenProvider
from zoho_inventory_mcp.auth.token_store import TokenStore
from zoho_inventory_mcp.client.errors import ZohoError
from zoho_inventory_mcp.client.http import ZohoClient
from zoho_inventory_mcp.config import Settings


@dataclass(frozen=True)
class DemoItem:
    sku: str
    name: str
    rate: float
    purchase_rate: float
    stock: int
    reorder_level: int


# Items whose stock is at or below reorder level are deliberate: list_low_stock_items should find them.
ITEMS = [
    DemoItem("DEMO-KUR-BLU-M", "Cotton Kurta - Indigo Blue (M)", 1299, 650, 42, 10),
    DemoItem("DEMO-KUR-BLU-L", "Cotton Kurta - Indigo Blue (L)", 1299, 650, 6, 10),  # low
    DemoItem("DEMO-KUR-MUS-M", "Linen Kurta - Mustard (M)", 1599, 820, 18, 8),
    DemoItem("DEMO-DUP-BLK", "Block Print Dupatta", 699, 300, 35, 10),
    DemoItem("DEMO-SAR-MAR", "Handloom Saree - Maroon", 3499, 1900, 4, 5),  # low
    DemoItem("DEMO-MUG-SET2", "Ceramic Chai Mug (Set of 2)", 549, 220, 60, 15),
    DemoItem("DEMO-DIYA-BR", "Brass Diya Pair", 799, 380, 12, 10),
    DemoItem("DEMO-TEA-MAS250", "Masala Chai Blend 250g", 349, 140, 3, 20),  # low
    DemoItem("DEMO-TEA-DRJ100", "Darjeeling Green Tea 100g", 449, 190, 25, 10),
    DemoItem("DEMO-BAG-JUTE", "Jute Tote Bag", 399, 150, 80, 20),
    DemoItem("DEMO-CHP-KOL8", "Leather Kolhapuri Chappal (Size 8)", 1899, 900, 9, 6),
    DemoItem("DEMO-CASE-MDB", "Phone Case - Madhubani Print", 499, 180, 0, 10),  # out of stock
    DemoItem("DEMO-OIL-COC500", "Cold-Pressed Coconut Oil 500ml", 399, 170, 40, 15),
    DemoItem("DEMO-SOAP-SDL3", "Sandalwood Soap (Pack of 3)", 299, 110, 55, 20),
    DemoItem("DEMO-BED-JPR-Q", "Jaipuri Cotton Bedsheet (Queen)", 1499, 700, 14, 5),
    DemoItem("DEMO-BTL-CU1L", "Copper Water Bottle 1L", 999, 450, 7, 8),  # low
    DemoItem("DEMO-BRUSH-BMB4", "Bamboo Toothbrush (Pack of 4)", 249, 90, 90, 25),
    DemoItem("DEMO-PLN-TER-M", "Terracotta Planter (Medium)", 649, 260, 22, 8),
    DemoItem("DEMO-STL-SLK-EM", "Silk Stole - Emerald", 1199, 560, 16, 5),
    DemoItem("DEMO-BOX-MSL", "Masala Dabba Spice Box", 1099, 520, 11, 4),
]


@dataclass(frozen=True)
class DemoContact:
    first: str
    last: str
    phone: str
    city: str
    state: str

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def email(self) -> str:
        return f"{self.first}.{self.last}@example.com".lower()


CONTACTS = [
    DemoContact("Priya", "Sharma", "+91 55500 01001", "Jaipur", "Rajasthan"),
    DemoContact("Arjun", "Mehta", "+91 55500 01002", "Mumbai", "Maharashtra"),
    DemoContact("Ananya", "Iyer", "+91 55500 01003", "Chennai", "Tamil Nadu"),
    DemoContact("Rohan", "Gupta", "+91 55500 01004", "New Delhi", "Delhi"),
    DemoContact("Kavya", "Nair", "+91 55500 01005", "Kochi", "Kerala"),
    DemoContact("Vikram", "Singh", "+91 55500 01006", "Lucknow", "Uttar Pradesh"),
    DemoContact("Sneha", "Patel", "+91 55500 01007", "Ahmedabad", "Gujarat"),
    DemoContact("Aditya", "Rao", "+91 55500 01008", "Bengaluru", "Karnataka"),
    DemoContact("Meera", "Joshi", "+91 55500 01009", "Pune", "Maharashtra"),
    DemoContact("Farhan", "Qureshi", "+91 55500 01010", "Hyderabad", "Telangana"),
]


@dataclass(frozen=True)
class DemoOrder:
    reference: str
    customer: str
    days_ago: int
    lines: tuple[tuple[str, int], ...]  # (sku, quantity)
    stage: str  # draft | confirmed | packed | shipped


ORDERS = [
    DemoOrder("DEMO-SO-01", "Priya Sharma", 9, (("DEMO-KUR-BLU-M", 1), ("DEMO-DUP-BLK", 1)), "shipped"),
    DemoOrder("DEMO-SO-02", "Arjun Mehta", 8, (("DEMO-MUG-SET2", 2),), "shipped"),
    DemoOrder("DEMO-SO-03", "Ananya Iyer", 7, (("DEMO-SAR-MAR", 1),), "shipped"),
    DemoOrder("DEMO-SO-04", "Rohan Gupta", 6, (("DEMO-TEA-DRJ100", 2), ("DEMO-MUG-SET2", 1)), "packed"),
    DemoOrder("DEMO-SO-05", "Kavya Nair", 5, (("DEMO-OIL-COC500", 3), ("DEMO-SOAP-SDL3", 2)), "packed"),
    DemoOrder("DEMO-SO-06", "Vikram Singh", 5, (("DEMO-CHP-KOL8", 1),), "confirmed"),
    DemoOrder("DEMO-SO-07", "Sneha Patel", 4, (("DEMO-BED-JPR-Q", 1), ("DEMO-PLN-TER-M", 2)), "confirmed"),
    DemoOrder("DEMO-SO-08", "Aditya Rao", 3, (("DEMO-BTL-CU1L", 2),), "confirmed"),
    DemoOrder("DEMO-SO-09", "Priya Sharma", 2, (("DEMO-KUR-MUS-M", 1), ("DEMO-STL-SLK-EM", 1)), "confirmed"),
    DemoOrder("DEMO-SO-10", "Meera Joshi", 2, (("DEMO-BOX-MSL", 1), ("DEMO-TEA-MAS250", 2)), "confirmed"),
    DemoOrder("DEMO-SO-11", "Farhan Qureshi", 1, (("DEMO-BAG-JUTE", 3),), "confirmed"),
    DemoOrder("DEMO-SO-12", "Arjun Mehta", 1, (("DEMO-KUR-BLU-L", 2),), "draft"),
    DemoOrder("DEMO-SO-13", "Kavya Nair", 0, (("DEMO-DIYA-BR", 2), ("DEMO-BRUSH-BMB4", 1)), "draft"),
    DemoOrder("DEMO-SO-14", "Rohan Gupta", 0, (("DEMO-CASE-MDB", 1),), "draft"),
    DemoOrder("DEMO-SO-15", "Sneha Patel", 0, (("DEMO-SAR-MAR", 1), ("DEMO-STL-SLK-EM", 2)), "draft"),
]


def item_payload(item: DemoItem) -> dict[str, Any]:
    return {
        "name": item.name,
        "sku": item.sku,
        "unit": "pcs",
        "item_type": "inventory",
        "product_type": "goods",
        "rate": item.rate,
        "purchase_rate": item.purchase_rate,
        "initial_stock": item.stock,
        "initial_stock_rate": item.purchase_rate,
        "reorder_level": item.reorder_level,
    }


def contact_payload(contact: DemoContact) -> dict[str, Any]:
    address = {"city": contact.city, "state": contact.state, "country": "India"}
    return {
        "contact_name": contact.name,
        "contact_type": "customer",
        "customer_sub_type": "individual",
        "billing_address": address,
        "shipping_address": address,
        "contact_persons": [
            {
                "first_name": contact.first,
                "last_name": contact.last,
                "email": contact.email,
                "mobile": contact.phone,
                "is_primary_contact": True,
            }
        ],
    }


class Seeder:
    def __init__(self, client: ZohoClient, *, dry_run: bool):
        self.client = client
        self.dry_run = dry_run
        self.created: dict[str, int] = {"items": 0, "contacts": 0, "salesorders": 0, "packages": 0, "shipments": 0}

    async def _create(self, path: str, body: dict[str, Any], key: str, params: dict | None = None) -> dict:
        if self.dry_run:
            return {f"{key}_id": f"dry-run-{len(self.created)}", "line_items": []}
        response = await self.client.post(path, body, params)
        return response[key]

    async def seed_items(self) -> dict[str, str]:
        existing, _ = await self.client.paginate("items", "items", max_pages=5, fresh=True)
        by_sku = {i.get("sku"): i["item_id"] for i in existing if i.get("sku")}
        for item in ITEMS:
            if item.sku in by_sku:
                continue
            created = await self._create("items", item_payload(item), "item")
            by_sku[item.sku] = created["item_id"]
            self.created["items"] += 1
            print(f"  + item {item.sku:<18} {item.name}")
        return by_sku

    async def seed_contacts(self) -> dict[str, str]:
        existing, _ = await self.client.paginate(
            "contacts", "contacts", {"filter_by": "Status.All"}, max_pages=5, fresh=True
        )
        by_name = {c["contact_name"]: c["contact_id"] for c in existing}
        for contact in CONTACTS:
            if contact.name in by_name:
                continue
            created = await self._create("contacts", contact_payload(contact), "contact")
            by_name[contact.name] = created["contact_id"]
            self.created["contacts"] += 1
            print(f"  + contact {contact.name}")
        return by_name

    async def seed_orders(self, items: dict[str, str], contacts: dict[str, str]) -> None:
        existing, _ = await self.client.paginate("salesorders", "salesorders", max_pages=5, fresh=True)
        by_reference = {o.get("reference_number"): o for o in existing}
        rates = {i.sku: i.rate for i in ITEMS}

        for order in ORDERS:
            order_date = (date.today() - timedelta(days=order.days_ago)).isoformat()
            summary = by_reference.get(order.reference)
            if summary is None:
                salesorder = await self._create(
                    "salesorders",
                    {
                        "customer_id": contacts[order.customer],
                        "reference_number": order.reference,
                        "date": order_date,
                        "shipment_date": (date.fromisoformat(order_date) + timedelta(days=3)).isoformat(),
                        "line_items": [
                            {"item_id": items[sku], "quantity": qty, "rate": rates[sku]} for sku, qty in order.lines
                        ],
                    },
                    "salesorder",
                )
                self.created["salesorders"] += 1
                print(f"  + order {order.reference} for {order.customer} -> {order.stage}")
            elif order.stage == "draft" or (
                order.stage == "confirmed" and summary.get("status") != "draft"
            ):
                continue  # already at the target stage; skip the detail fetch
            else:
                fetched = await self.client.get(f"salesorders/{summary['salesorder_id']}", fresh=True)
                salesorder = fetched.data["salesorder"]

            if not self.dry_run:
                await self.advance(order, salesorder, order_date)

    async def advance(self, order: DemoOrder, salesorder: dict[str, Any], order_date: str) -> None:
        """Move an order forward to its target stage, resuming from wherever it is now."""
        so_id = salesorder["salesorder_id"]
        number = order.reference.removeprefix("DEMO-SO-")
        if order.stage == "draft":
            return
        if salesorder.get("status") == "draft":
            await self.client.post(f"salesorders/{so_id}/status/confirmed", {})
        if order.stage == "confirmed":
            return

        packages = salesorder.get("packages") or []
        if packages:
            package_id = packages[0]["package_id"]
        else:
            package = await self._create(
                "packages",
                {
                    "package_number": f"DEMO-PKG-{number}",
                    "date": order_date,
                    "line_items": [
                        {"so_line_item_id": line["line_item_id"], "quantity": line["quantity"]}
                        for line in salesorder["line_items"]
                    ],
                },
                "package",
                params={"salesorder_id": so_id},
            )
            package_id = package["package_id"]
            self.created["packages"] += 1
            print(f"    packed   {order.reference}")
        if order.stage == "packed" or salesorder.get("shipped_status") == "shipped":
            return

        await self._create(
            "shipmentorders",
            {
                "shipment_number": f"DEMO-SHP-{number}",
                "date": order_date,
                "delivery_method": "Delhivery",
                "tracking_number": f"DEMO-TRK-{number}",
            },
            "shipmentorder",
            params={"package_ids": package_id, "salesorder_id": so_id},
        )
        self.created["shipments"] += 1
        print(f"    shipped  {order.reference}")


async def main(dry_run: bool) -> None:
    settings = Settings()
    async with httpx.AsyncClient(timeout=30) as http:
        tokens = TokenProvider(
            TokenStore(settings.token_dir, "seed"),
            http,
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
        client = ZohoClient(tokens, http, plan=settings.plan)
        seeder = Seeder(client, dry_run=dry_run)
        print(f"Seeding '{client.organization_name}' ({client.organization_id}){' [dry run]' if dry_run else ''}")
        try:
            print("Items:")
            items = await seeder.seed_items()
            print("Contacts:")
            contacts = await seeder.seed_contacts()
            print("Sales orders:")
            await seeder.seed_orders(items, contacts)
        except ZohoError as exc:
            print(f"\nZoho rejected a request: [{exc.kind}] {exc.message} (code {exc.zoho_code})")
            print("Fix the cause and re-run; records created so far are kept and will be skipped.")
            raise SystemExit(1) from None
        finally:
            print(f"\nCreated: {seeder.created}")
            print(f"API calls used: {client.stats.upstream_requests}; daily quota: {client.daily.snapshot()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Only read existing data; create nothing.")
    asyncio.run(main(parser.parse_args().dry_run))
