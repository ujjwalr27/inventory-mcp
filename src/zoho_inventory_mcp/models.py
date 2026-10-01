"""Trimmed views of Zoho records.

Raw Zoho records are huge (117 keys per item, 129 per contact, 170 per sales order).
Agents do better with a dozen clearly named fields, so each model keeps only what a
merchant-facing agent needs. Deliberately excluded:
  * payment data (paid_status, balance, receivables): out of scope for this connector
  * free-text fields (descriptions, notes, terms): rarely needed, and a prompt-injection
    vector because anyone who can edit a record controls that text
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


def _num(value: Any) -> float | int | None:
    """Zoho sends quantities as floats (42.0); show whole numbers as ints."""
    if value in (None, ""):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def _text(value: Any) -> str | None:
    return value or None


class Item(BaseModel):
    item_id: str
    name: str
    sku: str | None
    status: str
    unit: str | None
    selling_price: float | int | None
    cost_price: float | int | None
    stock_on_hand: float | int | None = Field(description="Physical stock in the warehouse.")
    available_for_sale: float | int | None = Field(
        description="Stock on hand minus quantity committed to confirmed orders."
    )
    committed_stock: float | int | None = Field(None, description="Only present on get_item.")
    reorder_level: float | int | None
    is_low_stock: bool = Field(description="True when available_for_sale is at or below reorder_level.")
    in_stock: bool

    @classmethod
    def from_zoho(cls, raw: dict[str, Any]) -> "Item":
        available = _num(raw.get("actual_available_for_sale_stock", raw.get("actual_available_stock")))
        if available is None:
            available = _num(raw.get("available_stock"))
        reorder = _num(raw.get("reorder_level"))
        return cls(
            item_id=str(raw["item_id"]),
            name=raw.get("name", ""),
            sku=_text(raw.get("sku")),
            status=raw.get("status", "unknown"),
            unit=_text(raw.get("unit")),
            selling_price=_num(raw.get("rate")),
            cost_price=_num(raw.get("purchase_rate")),
            stock_on_hand=_num(raw.get("stock_on_hand")),
            available_for_sale=available,
            committed_stock=_num(raw.get("committed_stock")),
            reorder_level=reorder,
            is_low_stock=bool(reorder) and available is not None and available <= reorder,
            in_stock=available is not None and available > 0,
        )


FulfillmentStage = Literal[
    "draft", "confirmed", "partially_packed", "packed", "partially_shipped", "shipped", "delivered", "void", "other"
]


def fulfillment_stage(status: str, quantity: float, packed: float, shipped: float) -> FulfillmentStage:
    """Zoho has no 'packed' status (a packed order is still 'confirmed'), so derive it."""
    status = (status or "").lower()
    if status in ("draft", "void", "delivered"):
        return status  # type: ignore[return-value]
    if quantity > 0 and shipped >= quantity:
        return "shipped"
    if shipped > 0:
        return "partially_shipped"
    if quantity > 0 and packed >= quantity:
        return "packed"
    if packed > 0:
        return "partially_packed"
    if status in ("confirmed", "open", "shipped", "partially_shipped", "fulfilled"):
        return "confirmed"
    return "other"


class SalesOrderSummary(BaseModel):
    salesorder_id: str
    order_number: str
    reference_number: str | None
    date: str | None
    customer_name: str
    customer_id: str | None
    status: str = Field(description="Zoho's own status: draft, confirmed, shipped, void, ...")
    fulfillment_stage: FulfillmentStage
    expected_shipment_date: str | None
    total: float | int | None
    currency: str | None
    invoiced_status: str | None

    @classmethod
    def _base(cls, raw: dict[str, Any], quantity: float, packed: float, shipped: float) -> dict[str, Any]:
        return dict(
            salesorder_id=str(raw["salesorder_id"]),
            order_number=raw.get("salesorder_number", ""),
            reference_number=_text(raw.get("reference_number")),
            date=_text(raw.get("date")),
            customer_name=raw.get("customer_name", ""),
            customer_id=_text(raw.get("customer_id")),
            status=raw.get("status") or raw.get("order_status") or "unknown",
            fulfillment_stage=fulfillment_stage(raw.get("status", ""), quantity, packed, shipped),
            expected_shipment_date=_text(raw.get("shipment_date")),
            total=_num(raw.get("total")),
            currency=_text(raw.get("currency_code")),
            invoiced_status=_text(raw.get("invoiced_status")),
        )

    @classmethod
    def from_zoho(cls, raw: dict[str, Any]) -> "SalesOrderSummary":
        return cls(
            **cls._base(
                raw,
                float(raw.get("quantity") or 0),
                float(raw.get("quantity_packed") or 0),
                float(raw.get("quantity_shipped") or 0),
            )
        )


class LineItem(BaseModel):
    item_id: str | None
    name: str
    sku: str | None
    quantity: float | int | None
    quantity_packed: float | int | None
    quantity_shipped: float | int | None
    rate: float | int | None
    amount: float | int | None


class Shipment(BaseModel):
    package_number: str | None
    status: str | None
    shipment_number: str | None
    carrier: str | None
    tracking_number: str | None
    shipped_on: str | None
    delivered_on: str | None


class SalesOrder(SalesOrderSummary):
    line_items: list[LineItem]
    shipments: list[Shipment] = Field(description="One entry per package, with shipment details once shipped.")
    delivery_method: str | None
    ship_to: str | None = Field(description="City and state only; full street addresses are not exposed.")

    @classmethod
    def from_zoho(cls, raw: dict[str, Any]) -> "SalesOrder":
        lines = raw.get("line_items") or []
        quantity = sum(float(li.get("quantity") or 0) for li in lines)
        packed = sum(float(li.get("quantity_packed") or 0) for li in lines)
        shipped = sum(float(li.get("quantity_shipped") or 0) for li in lines)

        shipments = []
        for package in raw.get("packages") or []:
            order = package.get("shipment_order") or {}
            shipments.append(
                Shipment(
                    package_number=_text(package.get("package_number")),
                    status=_text(package.get("shipment_status") or package.get("status")),
                    shipment_number=_text(package.get("shipment_number")),
                    carrier=_text(package.get("carrier") or package.get("delivery_method")),
                    tracking_number=_text(package.get("tracking_number")),
                    shipped_on=_text(package.get("shipment_date")),
                    delivered_on=_text(order.get("shipment_delivered_date") or order.get("delivery_date")),
                )
            )

        address = raw.get("shipping_address") or {}
        ship_to = ", ".join(part for part in (address.get("city"), address.get("state")) if part) or None

        return cls(
            **cls._base(raw, quantity, packed, shipped),
            line_items=[
                LineItem(
                    item_id=_text(li.get("item_id")),
                    name=li.get("name", ""),
                    sku=_text(li.get("sku")),
                    quantity=_num(li.get("quantity")),
                    quantity_packed=_num(li.get("quantity_packed")),
                    quantity_shipped=_num(li.get("quantity_shipped")),
                    rate=_num(li.get("rate")),
                    amount=_num(li.get("item_total")),
                )
                for li in lines
            ],
            shipments=shipments,
            delivery_method=_text(raw.get("delivery_method")),
            ship_to=ship_to,
        )


class Contact(BaseModel):
    contact_id: str
    name: str
    company: str | None
    email: str | None
    phone: str | None
    contact_type: str | None
    status: str | None
    location: str | None = Field(None, description="Billing city and state; only present on get_contact.")

    @classmethod
    def from_zoho(cls, raw: dict[str, Any]) -> "Contact":
        address = raw.get("billing_address") or {}
        location = ", ".join(part for part in (address.get("city"), address.get("state")) if part) or None
        return cls(
            contact_id=str(raw["contact_id"]),
            name=raw.get("contact_name", ""),
            company=_text(raw.get("company_name")),
            email=_text(raw.get("email")),
            phone=_text(raw.get("mobile") or raw.get("phone")),
            contact_type=_text(raw.get("contact_type")),
            status=_text(raw.get("status")),
            location=location,
        )
