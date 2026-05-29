from pydantic import BaseModel, Field


class LineItem(BaseModel):
    product_name: str
    vintage: int | None = None
    size: str = "750ml"
    quantity: float
    unit_type: str = "bottle"  # bottle, case
    base_unit_price_cents: int
    discount_percent: float = 0.0
    final_unit_price_cents: int
    line_total_cents: int
    bottles_per_case: int = 12
    confidence: float = 1.0
    notes: str | None = None


class InvoiceDraft(BaseModel):
    invoice_request_raw_text: str | None = None
    customer_name: str | None = None
    customer_company: str | None = None
    customer_email: str | None = None
    tier_name: str | None = None
    invoice_type: str = "estimate"  # estimate, invoice
    line_items: list[LineItem] = []
    subtotal_cents: int = 0
    discount_cents: int = 0
    tax_cents: int | None = None  # never guess tax
    shipping_cents: int | None = None
    total_before_tax_cents: int = 0
    payment_schedule: str = "NET_30"  # UPON_RECEIPT, NET_7, NET_14, NET_30
    accepted_payment_methods: list[str] = Field(
        default_factory=lambda: ["CARD", "BANK_ACCOUNT"]
    )
    status: str = "draft_ready"
    warnings: list[str] = []
    missing_fields: list[str] = []
    square_order_id: str | None = None
    square_invoice_id: str | None = None
    square_invoice_url: str | None = None
