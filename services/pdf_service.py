"""PDF text extraction via Claude's native document API.

Supports both digital PDFs (native text) and scanned PDFs (vision OCR).
No extra system deps — Claude handles it all.
"""
import base64

import anthropic

from app.config import ANTHROPIC_API_KEY

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_MODEL = "claude-haiku-4-5-20251001"


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Return all text extracted from a PDF, using Claude as the OCR engine."""
    b64 = base64.standard_b64encode(pdf_bytes).decode()
    response = _client.messages.create(
        model=_MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Extract all text from this document. "
                        "Return only the raw text content with no commentary."
                    ),
                },
            ],
        }],
    )
    return response.content[0].text


def extract_invoice_fields_from_pdf(pdf_bytes: bytes) -> str:
    """One-shot: read PDF and return a natural-language invoice request summary.

    Returns text ready to be fed directly into the invoice graph as raw_message.
    """
    b64 = base64.standard_b64encode(pdf_bytes).decode()
    response = _client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "This is an invoice request or order document. "
                        "Summarise it as a single natural-language invoice request: "
                        "who is the customer (name, email, company), "
                        "what products/wines they want (name, vintage, quantity, unit), "
                        "and any notes. "
                        "Write it as if a sales rep is describing the order."
                    ),
                },
            ],
        }],
    )
    return response.content[0].text
