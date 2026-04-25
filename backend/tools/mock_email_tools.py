"""
backend.tools.mock_email_tools

Mock implementation of the Gmail MCP (Model Context Protocol) email tool.

Phase 4 replacement:
    This module is replaced by a live Gmail MCP server integration in Phase 4.
    The MCP server will connect to the Gmail API, search the authenticated
    inbox for emails matching a configurable invoice query (e.g. subject
    contains "Invoice" or attachments with PDF), parse the email body and
    attachments, and return structured invoice dicts in the same schema
    defined here — so the Transform Agent requires zero changes when the
    swap happens.

Mock design:
    Returns 3 hardcoded invoices representative of real-world data variety:

    Invoice 1 (INV-2024-001) — Clean, fully valid invoice.
        All fields present. total_amount matches sum of line_items exactly.
        The Transform Agent should pass this through without errors.

    Invoice 2 (INV-2024-002) — Silent arithmetic corruption.
        All fields are present but total_amount does NOT match the sum of
        line_items (off by 150.00 — a common OCR / rounding error in real
        invoice parsing). The Transform Agent must catch this via a
        recalculation check.

    Invoice 3 (INV-2024-003) — Missing required field.
        total_amount key is entirely absent, simulating a parsing failure
        where the Gmail attachment was unreadable or incomplete. The
        Transform Agent must catch this via a required-field presence check.
"""

# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------

async def fetch_invoices_from_email() -> list[dict]:
    """
    Mock Gmail MCP tool — returns realistic fake invoice data extracted from emails.

    In Phase 4 this is replaced by an async call to the Gmail MCP server which
    authenticates with OAuth 2.0, fetches matching emails, and parses PDF
    attachments using a document extraction model.

    Returns:
        List of invoice dicts. Each complete invoice contains:
            invoice_id    : str   — unique invoice reference number
            vendor_name   : str   — company issuing the invoice
            customer_name : str   — company being billed
            customer_id   : str   — internal customer identifier
            line_items    : list  — itemised charges
                description : str
                quantity    : int
                unit_price  : float
            total_amount  : float — declared total (may be absent or incorrect)
            invoice_date  : str   — ISO 8601 date (YYYY-MM-DD)
            due_date      : str   — ISO 8601 date (YYYY-MM-DD)
            currency      : str   — ISO 4217 currency code
            status        : str   — one of: pending | paid | unpaid

    Intentional validation failures for Transform Agent testing:
        INV-2024-002 — total_amount does not match sum of line_items
        INV-2024-003 — total_amount field is missing entirely
    """

    invoices = [

        # ── Invoice 1: Clean, fully valid ────────────────────────────────────
        # All fields present. total_amount = sum(qty * unit_price) = 4,850.00
        # Transform Agent should accept without errors.
        {
            "invoice_id":    "INV-2024-001",
            "vendor_name":   "Vertex Cloud Solutions Ltd.",
            "customer_name": "Meridian Analytics Inc.",
            "customer_id":   "CUST-10042",
            "line_items": [
                {
                    "description": "Cloud Infrastructure — Standard Tier (30 days)",
                    "quantity":    1,
                    "unit_price":  3200.00,
                },
                {
                    "description": "Managed Security Monitoring Service",
                    "quantity":    2,
                    "unit_price":  650.00,
                },
                {
                    "description": "Technical Support — Priority SLA",
                    "quantity":    1,
                    "unit_price":  350.00,
                },
            ],
            "total_amount":  4850.00,   # correct: 3200 + (2 × 650) + 350
            "invoice_date":  "2024-03-01",
            "due_date":      "2024-03-31",
            "currency":      "USD",
            "status":        "pending",
        },

        # ── Invoice 2: Silent arithmetic mismatch ─────────────────────────────
        # total_amount is declared as 2,890.00 but true sum is 3,040.00.
        # Discrepancy of -150.00 — typical of OCR misread or manual entry error.
        # Transform Agent must recalculate and flag the mismatch.
        {
            "invoice_id":    "INV-2024-002",
            "vendor_name":   "Apex Data Logistics GmbH",
            "customer_name": "Meridian Analytics Inc.",
            "customer_id":   "CUST-10042",
            "line_items": [
                {
                    "description": "Cross-border Data Pipeline — Monthly Subscription",
                    "quantity":    1,
                    "unit_price":  1800.00,
                },
                {
                    "description": "ETL Processing Units (per 100k records)",
                    "quantity":    4,
                    "unit_price":  310.00,
                },
            ],
            "total_amount":  2890.00,   # WRONG — true sum is 1800 + (4 × 310) = 3040.00
            "invoice_date":  "2024-03-05",
            "due_date":      "2024-04-04",
            "currency":      "EUR",
            "status":        "unpaid",
        },

        # ── Invoice 3: Missing required field ─────────────────────────────────
        # total_amount key is completely absent — simulates a PDF parse failure
        # where the invoice total could not be extracted from the attachment.
        # Transform Agent must detect the missing field and flag for manual review.
        {
            "invoice_id":    "INV-2024-003",
            "vendor_name":   "NovaPrint Office Supplies",
            "customer_name": "Meridian Analytics Inc.",
            "customer_id":   "CUST-10042",
            "line_items": [
                {
                    "description": "A4 Premium Paper — 500 sheets × 5 reams",
                    "quantity":    5,
                    "unit_price":  28.50,
                },
                {
                    "description": "Laser Toner Cartridge — High Yield Black",
                    "quantity":    3,
                    "unit_price":  74.99,
                },
                {
                    "description": "Ergonomic Desk Mat (XL)",
                    "quantity":    10,
                    "unit_price":  22.00,
                },
            ],
            # total_amount intentionally omitted — PDF extraction failed
            "invoice_date":  "2024-03-10",
            "due_date":      "2024-03-25",
            "currency":      "GBP",
            "status":        "pending",
        },

    ]

    return invoices
