"""
backend.tools.mock_api_tools

Mock implementation of the CRM API MCP tool.

Phase 4 replacement:
    This module is replaced by a live CRM MCP server integration in Phase 4.
    The MCP server will authenticate with the target CRM (e.g. Salesforce,
    HubSpot, Zoho) via OAuth 2.0, call the CRM's REST API to create or
    update an invoice/opportunity record, and return the CRM-assigned record ID.

    The integration_agent.py import path stays the same — only this file is
    swapped. The function signature (invoice: dict) -> str is preserved so
    the agent requires no changes.

Mock behaviour:
    Always succeeds. Logs the invoice being "pushed" and returns a
    deterministic fake CRM record ID derived from the invoice_id.
    No actual HTTP request is made.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def push_invoice_to_crm(invoice: dict) -> str:
    """
    Mock CRM API tool — simulates pushing an invoice record to a CRM system.

    Logs the operation and returns a fake CRM record ID. In Phase 4 this is
    replaced by a real MCP CRM connector that authenticates via OAuth 2.0 and
    calls the CRM's invoice/opportunity creation endpoint.

    Args:
        invoice: Validated invoice dict from the transform agent. Expected to
                 contain at minimum: invoice_id, vendor_name, total_amount.

    Returns:
        A fake CRM record ID string in the format "crm-record-{invoice_id}".
    """
    # Yield to the event loop — makes this mock behave like real async I/O.
    # Without this, an async def that does no actual I/O runs synchronously,
    # which can mask concurrency bugs if the integration agent runs multiple
    # tool calls in parallel in a later phase.
    await asyncio.sleep(0)

    invoice_id: str = invoice.get("invoice_id", "unknown")
    vendor: str = invoice.get("vendor_name", "unknown vendor")
    total: float = invoice.get("total_amount", 0.0)
    currency: str = invoice.get("currency", "USD")

    logger.info(
        "mock_api_tools: [MOCK] pushing invoice '%s' from '%s' "
        "(total: %.2f %s) to CRM.",
        invoice_id,
        vendor,
        total,
        currency,
    )

    crm_record_id = f"crm-record-{invoice_id}"

    logger.info(
        "mock_api_tools: [MOCK] CRM push confirmed — crm_record_id=%s.",
        crm_record_id,
    )

    return crm_record_id
