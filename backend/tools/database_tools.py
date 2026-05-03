"""
backend.tools.database_tools

Real implementation of the database write tool for the integration agent.

This module writes the final business record for a processed invoice to the
``invoice_records`` Supabase table.  It is deliberately distinct from the
pipeline artifact tables (``extracted_invoices``, ``transformed_invoices``,
``integration_results``) that track internal workflow progress:

    Pipeline artifact tables  — track *how* data moved through the workflow.
        extracted_invoices     Raw email payloads; written by extraction_agent.
        transformed_invoices   Validated/normalised payloads; written by transform_agent.
        integration_results    Tool call outcomes (DB + CRM); written by integration_agent.

    Business record table     — the *output* of the workflow.
        invoice_records        One row per successfully processed invoice.
                               This is what downstream finance systems, reporting
                               dashboards, and audits consume.  It must exist
                               regardless of whether the CRM push succeeded.

Required Supabase table (run once, see SQL below):
    CREATE TABLE IF NOT EXISTS invoice_records (
        id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id   TEXT        NOT NULL,
        invoice_id    TEXT        NOT NULL UNIQUE,
        vendor_name   TEXT,
        customer_name TEXT,
        total_amount  FLOAT,
        currency      TEXT,
        status        TEXT,
        data          JSONB,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );

Idempotency:
    The insert uses ON CONFLICT (invoice_id) DO NOTHING so that LangGraph
    retries (which re-run the full node) do not produce duplicate rows.
    The RETURNING clause still fires on a genuine insert; a conflict returns
    no row, in which case a follow-up SELECT retrieves the existing id.
"""

import json
import logging

from backend.db.pool import get_pool

logger = logging.getLogger(__name__)


async def insert_invoice_to_db(invoice: dict) -> str:
    """
    Insert a validated invoice as a final business record into ``invoice_records``.

    This is the authoritative, human-readable record of the processed invoice.
    It differs from ``integration_results`` (which records tool-call outcomes)
    and ``transformed_invoices`` (which stores the intermediate pipeline
    artifact).  Downstream finance reporting queries this table directly.

    Idempotent: if a row with the same ``invoice_id`` already exists (e.g.
    due to a LangGraph node retry), the existing row is silently preserved and
    its ``id`` is returned unchanged.

    Args:
        invoice: Validated invoice dict from the transform agent.  Expected
                 keys: ``invoice_id``, ``vendor_name``, ``customer_name``,
                 ``total_amount``, ``currency``, ``status``.  Any extra keys
                 are captured wholesale in the ``data`` JSONB column.

    Returns:
        UUID string of the inserted (or pre-existing) ``invoice_records`` row.
        The caller (integration_agent) stores this as ``db_confirmation_id``
        in the ``integration_results`` table.

    Raises:
        psycopg.*: Any database infrastructure error — propagated so the
                   integration_agent can record a partial failure and the
                   LangGraph error handler can retry the node.
    """
    invoice_id: str = invoice.get("invoice_id", "unknown")
    vendor_name: str = invoice.get("vendor_name", "")
    customer_name: str = invoice.get("customer_name", "")
    total_amount: float = invoice.get("total_amount", 0.0)
    currency: str = invoice.get("currency", "USD")
    status: str = invoice.get("status", "processed")

    # Serialise the full invoice payload into the JSONB column for auditing.
    # psycopg3 accepts a Python dict directly for JSONB columns; json.dumps
    # is used here to produce a clean string that psycopg3 will re-parse,
    # avoiding any issues with non-serialisable types in nested values.
    data_json: str = json.dumps(invoice, default=str)

    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                INSERT INTO invoice_records
                    (workflow_id, invoice_id, vendor_name, customer_name,
                     total_amount, currency, status, data)
                VALUES
                    (%(workflow_id)s, %(invoice_id)s, %(vendor_name)s,
                     %(customer_name)s, %(total_amount)s, %(currency)s,
                     %(status)s, %(data)s::jsonb)
                ON CONFLICT (invoice_id) DO NOTHING
                RETURNING id::text
                """,
                {
                    "workflow_id": invoice.get("workflow_id", ""),
                    "invoice_id": invoice_id,
                    "vendor_name": vendor_name,
                    "customer_name": customer_name,
                    "total_amount": total_amount,
                    "currency": currency,
                    "status": status,
                    "data": data_json,
                },
            )
            row = await cur.fetchone()

    if row is not None:
        confirmation_id: str = row["id"]
        logger.info(
            "database_tools: invoice_records row inserted — invoice_id='%s', id=%s.",
            invoice_id,
            confirmation_id,
        )
    else:
        # ON CONFLICT DO NOTHING — row already exists; retrieve its id.
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id::text FROM invoice_records WHERE invoice_id = %s",
                (invoice_id,),
            )
            existing = await cur.fetchone()
            confirmation_id = existing["id"] if existing else invoice_id
            logger.info(
                "database_tools: invoice_id='%s' already in invoice_records — "
                "returning existing id=%s (idempotent retry).",
                invoice_id,
                confirmation_id,
            )

    return confirmation_id
