"""
backend.agents.extraction_agent

LangGraph node — Extraction Agent.

Responsibility:
    Runs after coordinator_node. Finds the EXTRACTION task from state["tasks"],
    calls the email tool to fetch invoice data, and persists each invoice as a
    JSONB row in the Supabase `extracted_invoices` table. Returns the inserted
    row UUIDs so downstream agents can retrieve full data without re-querying.

DB connection strategy — shared psycopg3 pool:
    This agent does NOT manage its own database connection pool. Instead it calls
    `backend.db.pool.get_pool()` to borrow from the single application-wide
    psycopg3 AsyncConnectionPool opened in the FastAPI lifespan hook (main.py).

    Connection usage in this agent:
        pool = get_pool()
        async with pool.connection() as conn:
            # conn is an async psycopg3 connection — use conn.execute(),
            # conn.fetchone(), conn.fetchall(), conn.transaction(), etc.

Phase 4 (complete):
    fetch_invoices_from_email() now connects to the Gmail API via OAuth2,
    fetches matching emails, and uses Gemini structured output to extract
    invoice fields.  The rest of this node (DB persistence, state update)
    is unchanged.

Required Supabase table (run once before Phase 2):
    CREATE TABLE IF NOT EXISTS extracted_invoices (
        id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id TEXT        NOT NULL,
        data        JSONB       NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""

import json
import logging
from typing import Optional

from backend.db.pool import get_pool
from backend.orchestration.state_manager import WorkflowState
from backend.tools.email_tools import fetch_invoices_from_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_extraction_task(tasks: list[dict]) -> Optional[dict]:
    """
    Locate the EXTRACTION task in the current task list.

    Returns the first task dict whose agent_type is "extraction",
    or None if no such task exists in state.

    Args:
        tasks: The list of lightweight task metadata dicts from WorkflowState.

    Returns:
        Matching task dict, or None.
    """
    for task in tasks:
        if task.get("agent_type") == "extraction":
            return task
    return None


async def _insert_invoice(
    conn,
    workflow_id: str,
    invoice: dict,
) -> str:
    """
    Insert a single invoice dict as a JSONB row into extracted_invoices.

    Args:
        conn:        Active psycopg3 async connection from the shared pool.
        workflow_id: UUID string of the parent workflow run.
        invoice:     Invoice dict returned by fetch_invoices_from_email().

    Returns:
        The UUID string of the newly inserted row.
    """
    cur = await conn.execute(
        """
        INSERT INTO extracted_invoices (workflow_id, data)
        VALUES (%s, %s::jsonb)
        RETURNING id::text
        """,
        (workflow_id, json.dumps(invoice)),  # psycopg3 expects a JSON string for ::jsonb cast
    )
    row = await cur.fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

async def extraction_node(state: WorkflowState) -> dict:
    """
    LangGraph extraction node — fetches invoices and persists them to Supabase.

    Steps:
        1. Locate the EXTRACTION task in state["tasks"].
        2. Call fetch_invoices_from_email() to retrieve raw invoice dicts.
        3. Write each invoice as a JSONB row to `extracted_invoices` in Supabase
           using the shared psycopg3 pool (backend.db.pool.get_pool()).
        4. Return the inserted row IDs and mark the extraction task as complete.

    Reads from state:
        state["workflow_id"]: Used to namespace every DB row.
        state["tasks"]:       Searched for an EXTRACTION task to mark complete.

    Returns:
        {
            "extracted_data_ids": list[str]  — Supabase row UUIDs, one per invoice.
                                               Appended via operator.add reducer.
            "completed_task_ids": list[str]  — The extraction task ID marked done.
                                               Appended via operator.add reducer.
            "status":             str        — "extraction_complete"
        }

    Raises:
        RuntimeError:  If no EXTRACTION task is found in state["tasks"].
        psycopg.*:     Any DB connection or query error — propagates to the
                       graph's error handler for retry via execute_with_retry().
    """
    workflow_id: str = state["workflow_id"]

    # ── Step 1: Find the extraction task ─────────────────────────────────────
    extraction_task = _find_extraction_task(state.get("tasks", []))

    if extraction_task is None:
        raise RuntimeError(
            f"extraction_node: no EXTRACTION task found in state for "
            f"workflow_id={workflow_id}. Coordinator may not have planned one."
        )

    task_id: str = extraction_task["id"]
    logger.info(
        "extraction_node: starting — workflow_id=%s, task_id=%s.",
        workflow_id,
        task_id,
    )

    # ── Step 2: Fetch invoices from the email tool ────────────────────────────
    logger.info("extraction_node: calling fetch_invoices_from_email().")
    invoices: list[dict] = await fetch_invoices_from_email()
    logger.info("extraction_node: received %d invoice(s) from email tool.", len(invoices))

    # ── Step 3: Persist each invoice to Supabase ──────────────────────────────
    # Use the application-wide shared psycopg3 pool. All inserts run inside a
    # single transaction so either all invoices are persisted or none are —
    # a partial write would leave the workflow in an inconsistent state.
    pool = get_pool()
    inserted_ids: list[str] = []

    async with pool.connection() as conn:
        async with conn.transaction():
            for invoice in invoices:
                row_id = await _insert_invoice(conn, workflow_id, invoice)
                inserted_ids.append(row_id)
                logger.info(
                    "extraction_node: inserted invoice '%s' → row_id=%s.",
                    invoice.get("invoice_id", "unknown"),
                    row_id,
                )

    logger.info(
        "extraction_node: persisted %d row(s) for workflow_id=%s.",
        len(inserted_ids),
        workflow_id,
    )

    # ── Step 4: Return state updates ─────────────────────────────────────────
    # Both lists use operator.add reducers — LangGraph appends these to whatever
    # other agents have already written, rather than overwriting their values.
    return {
        "extracted_data_ids": inserted_ids,
        "completed_task_ids": [task_id],
        "status": "extraction_complete",
    }
