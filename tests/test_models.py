import json

import pytest

from zoho_inventory_mcp.models import Contact, Item, SalesOrder, SalesOrderSummary, fulfillment_stage

from helpers import fixture

PAYMENT_FIELDS = {"paid_status", "balance", "outstanding_receivable_amount", "payments", "invoices"}


def test_item_detail_is_trimmed_and_readable():
    raw = fixture("item_detail")["item"]
    item = Item.from_zoho(raw)
    assert item.sku == "DEMO-KUR-BLU-M"
    assert item.stock_on_hand == 42 and isinstance(item.stock_on_hand, int)  # 42.0 -> 42
    assert item.available_for_sale == 41  # one unit committed to a confirmed order
    assert item.committed_stock == 1
    assert item.reorder_level == 10
    assert item.in_stock and not item.is_low_stock
    assert len(item.model_dump()) < 15 < len(raw)


def test_low_stock_fixture_matches_seeded_items():
    items = [Item.from_zoho(raw) for raw in fixture("items_lowstock")["items"]]
    assert {i.sku for i in items} == {
        "DEMO-KUR-BLU-L",
        "DEMO-SAR-MAR",
        "DEMO-TEA-MAS250",
        "DEMO-CASE-MDB",
        "DEMO-BTL-CU1L",
    }
    assert all(i.is_low_stock for i in items)
    assert not next(i for i in items if i.sku == "DEMO-CASE-MDB").in_stock


def test_order_list_derives_fulfillment_stage():
    orders = [SalesOrderSummary.from_zoho(raw) for raw in fixture("salesorders_list")["salesorders"]]
    stage = {o.reference_number: o.fulfillment_stage for o in orders}
    assert [stage[f"DEMO-SO-{n:02d}"] for n in (1, 2, 3)] == ["shipped"] * 3
    assert [stage[f"DEMO-SO-{n:02d}"] for n in (4, 5)] == ["packed"] * 2  # Zoho itself says "confirmed"
    assert [stage[f"DEMO-SO-{n:02d}"] for n in range(6, 12)] == ["confirmed"] * 6
    assert [stage[f"DEMO-SO-{n:02d}"] for n in range(12, 16)] == ["draft"] * 4


def test_shipped_order_detail_has_tracking_and_no_payment_data():
    raw = fixture("salesorder_detail_shipped")["salesorder"]
    order = SalesOrder.from_zoho(raw)
    assert order.fulfillment_stage == "shipped"
    assert order.customer_name == "Priya Sharma"
    assert [li.sku for li in order.line_items] == ["DEMO-KUR-BLU-M", "DEMO-DUP-BLK"]
    assert order.shipments[0].carrier == "Delhivery"
    assert order.shipments[0].tracking_number == "DEMO-TRK-01"
    assert order.ship_to == "Jaipur, Rajasthan"

    dumped = order.model_dump()
    assert not PAYMENT_FIELDS & dumped.keys()
    # The point of trimming: the agent gets a fraction of the raw payload.
    assert len(json.dumps(dumped)) < len(json.dumps(raw)) / 5


def test_packed_order_detail():
    order = SalesOrder.from_zoho(fixture("salesorder_detail_packed")["salesorder"])
    assert order.status == "confirmed"
    assert order.fulfillment_stage == "packed"
    assert order.shipments[0].status == "packed"
    assert order.shipments[0].tracking_number is None


def test_contact_detail():
    raw = fixture("contact_detail")["contact"]
    contact = Contact.from_zoho(raw)
    assert contact.email == "priya.sharma@example.com"
    assert contact.phone == "+91 55500 01001"  # falls back to mobile
    assert contact.location == "Jaipur, Rajasthan"
    assert not PAYMENT_FIELDS & contact.model_dump().keys()


@pytest.mark.parametrize(
    ("status", "qty", "packed", "shipped", "expected"),
    [
        ("draft", 3, 0, 0, "draft"),
        ("confirmed", 3, 0, 0, "confirmed"),
        ("confirmed", 3, 1, 0, "partially_packed"),
        ("confirmed", 3, 3, 0, "packed"),
        ("confirmed", 3, 3, 1, "partially_shipped"),
        ("shipped", 3, 3, 3, "shipped"),
        ("void", 3, 0, 0, "void"),
        ("onhold", 3, 0, 0, "other"),
    ],
)
def test_fulfillment_stage(status, qty, packed, shipped, expected):
    assert fulfillment_stage(status, qty, packed, shipped) == expected
