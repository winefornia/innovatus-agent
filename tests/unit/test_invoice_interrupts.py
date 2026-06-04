from services.invoice_interrupts import current_invoice_interrupt


def test_missing_fields_interrupt():
    assert current_invoice_interrupt({"missing_fields": ["customer"]}) == "missing"


def test_fuzzy_customer_interrupt():
    state = {"customer": {"full_name": "Jane"}, "customer_confirmed": False}
    assert current_invoice_interrupt(state) == "confirm_customer"


def test_tier_interrupt():
    state = {"customer": {"full_name": "Jane"}, "customer_confirmed": True}
    assert current_invoice_interrupt(state) == "tier"


def test_edit_instruction_interrupt():
    state = {"approval": "edit_requested", "invoice_preview": {"total_before_tax_cents": 100}}
    assert current_invoice_interrupt(state) == "edit_instruction"


def test_low_confidence_edit_clarification_interrupt():
    state = {
        "edit_instruction": "make it six",
        "invoice_preview": {"total_before_tax_cents": 100},
        "edit_patch": {"field_changes": [{"confidence": 0.5}]},
    }
    assert current_invoice_interrupt(state) == "edit_clarification"


def test_approval_interrupt():
    state = {"invoice_preview": {"total_before_tax_cents": 100}}
    assert current_invoice_interrupt(state) == "approval"


def test_send_interrupt():
    assert current_invoice_interrupt({"square_invoice_id": "inv_1"}) == "send"


def test_email_interrupt():
    state = {"square_invoice_id": "inv_1", "send_decision": "send"}
    assert current_invoice_interrupt(state) == "email"
