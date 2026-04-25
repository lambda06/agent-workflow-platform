"""
backend.tools.mock_database_tools

Mock implementation of the database write MCP tool.

Phase 4 replacement:
    This module is replaced by a live Database MCP server integration in Phase 4.
    The MCP server will connect to the target operational database (e.g. a
    finance ERP, a Postgres data warehouse, or a data lake sink) via a
    configured MCP connector, performing validated UPSERT operations using
    the invoice_id as the idempotency key.

    The integration_agent.py import path stays the same — only this file is
    swapped. The function signature (invoice: dict) -> str is preserved so
    the agent requires no changes.

Mock behaviour:
    Always succeeds. Logs the invoice being "written" and returns a
    deterministic fake confirmation ID derived from the invoice_id.
    No actual DB connection is made.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def insert_invoice_to_db(invoice: dict) -> str:
    """
    Mock database write tool — simulates inserting an invoice record.

    Logs the operation and returns a fake confirmation ID. In Phase 4 this
    is replaced by a real MCP database connector call that performs an
    authenticated UPSERT against the target operational database.

    Args:
        invoice: Validated invoice dict from the transform agent. Expected to
                 contain at minimum: invoice_id, vendor_name, total_amount.

    Returns:
        A fake confirmation ID string in the format "db-confirm-{invoice_id}".
    """
    # Yield to the event loop — makes this mock behave like real async I/O.
    # Without this, an async def that does no actual I/O runs synchronously,
    # which can mask concurrency bugs if the integration agent runs multiple
    # tool calls in parallel in a later phase.
    await asyncio.sleep(0)

    invoice_id: str = invoice.get("invoice_id", "unknown")
    vendor: str = invoice.get("vendor_name", "unknown vendor")

    logger.info(
        "mock_database_tools: [MOCK] inserting invoice '%s' from '%s' into database.",
        invoice_id,
        vendor,
    )

    confirmation_id = f"db-confirm-{invoice_id}"

    logger.info(
        "mock_database_tools: [MOCK] DB write confirmed — confirmation_id=%s.",
        confirmation_id,
    )

    return confirmation_id
