"""
backend.agents.integration_agent

LangGraph node — Integration Agent.

Responsibility:
    Runs after transform_node. Fetches validated invoices from Supabase
    transformed_invoices using the transformed_data_ids in state, calls both
    external integration tools concurrently per invoice, and persists the
    combined result to integration_results in Supabase.

Concurrency model — asyncio.gather per invoice:
    For each invoice, insert_invoice_to_db() and push_invoice_to_crm() are
    independent — neither call depends on the other's output. Running them
    sequentially would double the latency for no benefit. asyncio.gather()
    launches both coroutines concurrently and waits for both to finish before
    recording the combined result.

    return_exceptions=True is used so that if one tool call fails (e.g. the
    CRM push raises a timeout), gather() still returns the result of the other
    call (the DB confirmation ID). This lets us record a partial result row
    rather than losing all output from both tools on a single failure. The
    exception is then re-raised after the row is written so error_handler.py
    can decide whether to retry.

DB connection strategy — shared psycopg3 pool:
    This agent does NOT manage its own database connection pool. Instead it calls
    `backend.db.pool.get_pool()` to borrow from the single application-wide
    psycopg3 AsyncConnectionPool opened in the FastAPI lifespan hook (main.py).

    Connection usage in this agent:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                ...  # per-invoice result write

Required Supabase table (run once before Phase 2):
    CREATE TABLE IF NOT EXISTS integration_results (
        id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id        TEXT        NOT NULL,
        invoice_id         TEXT        NOT NULL,
        db_confirmation_id TEXT,
        crm_record_id      TEXT,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""

import asyncio
import json
import logging
from typing import Optional

from backend.db.pool import get_pool
from backend.orchestration.state_manager import WorkflowState
from backend.tools.api_tools import push_invoice_to_crm
from backend.tools.database_tools import insert_invoice_to_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_integration_task(tasks: list[dict]) -> Optional[dict]:
    """
    Locate the INTEGRATION task in the current state task list.

    Args:
        tasks: Lightweight task metadata dicts from WorkflowState.

    Returns:
        First task dict with agent_type == "integration", or None.
    """
    for task in tasks:
        if task.get("agent_type") == "integration":
            return task
    return None


async def _fetch_transformed_invoice(
    conn,
    row_id: str,
) -> Optional[dict]:
    """
    Fetch a single transformed_invoices row and return its JSONB data field.

    Args:
        conn:   Active psycopg3 async connection from the shared pool.
        row_id: UUID string of the transformed_invoices row.

    Returns:
        Parsed invoice dict, or None if the row does not exist.
    """
    cur = await conn.execute(
        "SELECT data FROM transformed_invoices WHERE id = %s::uuid",
        (row_id,),
    )
    record = await cur.fetchone()
    if record is None:
        return None
    # psycopg3 returns JSONB columns as a Python dict directly —
    # no manual json.loads() needed (unlike asyncpg which returned strings).
    raw = record["data"]
    return raw if isinstance(raw, dict) else json.loads(raw)


async def _insert_integration_result(
    conn,
    workflow_id: str,
    invoice_id: str,
    db_confirmation_id: Optional[str],
    crm_record_id: Optional[str],
) -> str:
    """
    Write the combined DB + CRM integration outcome to integration_results.

    Both confirmation IDs are nullable — if one tool call failed and returned
    an exception via asyncio.gather(return_exceptions=True), its corresponding
    column is stored as NULL so the row still records the partial success of
    the other tool.

    Args:
        conn:               Active psycopg3 async connection from the shared pool.
        workflow_id:        UUID string of the parent workflow run.
        invoice_id:         The invoice_id field of the processed invoice.
        db_confirmation_id: Return value of insert_invoice_to_db(), or None on failure.
        crm_record_id:      Return value of push_invoice_to_crm(), or None on failure.

    Returns:
        UUID string of the newly inserted integration_results row.
    """
    cur = await conn.execute(
        """
        INSERT INTO integration_results
            (workflow_id, invoice_id, db_confirmation_id, crm_record_id)
        VALUES (%s, %s, %s, %s)
        RETURNING id::text
        """,
        (workflow_id, invoice_id, db_confirmation_id, crm_record_id),
    )
    row = await cur.fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

async def integration_node(state: WorkflowState) -> dict:
    """
    LangGraph integration node — pushes validated invoices to external systems.

    Steps:
        1. Locate the INTEGRATION task in state["tasks"].
        2. For each ID in state["transformed_data_ids"], fetch the validated
           invoice from Supabase transformed_invoices using the shared psycopg3 pool.
        3. Call insert_invoice_to_db() and push_invoice_to_crm() concurrently
           via asyncio.gather(return_exceptions=True).
        4. Write the combined result (both confirmation IDs) to integration_results.
        5. Return integration_result_ids and mark the task complete.

    Why asyncio.gather per invoice (not sequential):
        insert_invoice_to_db() and push_invoice_to_crm() are completely
        independent — neither reads the other's output. Running them sequentially
        wastes wall-clock time proportional to the number of invoices. gather()
        runs both in the same event loop tick with zero threading overhead.

    Why return_exceptions=True:
        If the CRM push times out after the DB write has already resolved,
        gather() with the default return_exceptions=False would raise and lose
        the DB confirmation ID. With return_exceptions=True, both results are
        always captured — successful returns come back as strings, failures as
        exception objects. We write the partial result row (NULL for the failed
        tool), then re-raise so error_handler.py can retry the full node if
        configured to do so.

    Reads from state:
        state["workflow_id"]           : Namespace for all DB rows.
        state["tasks"]                 : Searched for the INTEGRATION task.
        state["transformed_data_ids"]  : Row IDs to process.

    Returns:
        {
            "integration_result_ids": list[str]  — integration_results row UUIDs.
            "completed_task_ids":     list[str]  — The integration task ID.
            "status":                 str        — "integration_complete"
        }

    Raises:
        RuntimeError:  If no INTEGRATION task is found in state["tasks"].
        psycopg.*:     Any DB infrastructure error — propagated for retry.
        Exception:     Re-raised after partial write if any tool call failed.
    """
    workflow_id: str = state["workflow_id"]
    transformed_ids: list[str] = state.get("transformed_data_ids", [])

    # ── Step 1: Find the integration task ────────────────────────────────────
    integration_task = _find_integration_task(state.get("tasks", []))

    if integration_task is None:
        raise RuntimeError(
            f"integration_node: no INTEGRATION task found in state for "
            f"workflow_id={workflow_id}. Coordinator may not have planned one."
        )

    task_id: str = integration_task["id"]
    logger.info(
        "integration_node: starting — workflow_id=%s, task_id=%s, invoices_to_integrate=%d.",
        workflow_id,
        task_id,
        len(transformed_ids),
    )

    if not transformed_ids:
        logger.warning(
            "integration_node: transformed_data_ids is empty for workflow_id=%s. "
            "Nothing to integrate — all invoices may have failed validation.",
            workflow_id,
        )
        return {
            "integration_result_ids": [],
            "completed_task_ids": [task_id],
            "status": "integration_complete",
        }

    # ── Steps 2–4: Fetch, integrate, and persist each invoice ────────────────
    pool = get_pool()
    integration_result_ids: list[str] = []
    tool_failures: list[Exception] = []   # collected to re-raise after all rows written

    async with pool.connection() as conn:
        for row_id in transformed_ids:

            # Fetch the validated invoice from transformed_invoices
            invoice = await _fetch_transformed_invoice(conn, row_id)

            if invoice is None:
                logger.warning(
                    "integration_node: transformed_invoices row '%s' not found — skipping.",
                    row_id,
                )
                continue

            invoice_id: str = invoice.get("invoice_id", "unknown")

            # ── Step 3: Concurrent tool calls ─────────────────────────────────
            #
            # asyncio.gather launches both coroutines concurrently in the same
            # event-loop iteration. return_exceptions=True means both always
            # complete — an exception from one tool is returned as an exception
            # object rather than immediately propagating and losing the other
            # tool's result.
            logger.info(
                "integration_node: calling DB and CRM tools concurrently for invoice '%s'.",
                invoice_id,
            )

            invoice["workflow_id"] = workflow_id

            db_result, crm_result = await asyncio.gather(
                insert_invoice_to_db(invoice),
                push_invoice_to_crm(invoice),
                return_exceptions=True,
            )

            # Unpack results — distinguish successful strings from exceptions
            db_confirmation_id: Optional[str] = None
            crm_record_id: Optional[str] = None

            if isinstance(db_result, Exception):
                logger.error(
                    "integration_node: insert_invoice_to_db failed for '%s': %s",
                    invoice_id, db_result,
                )
                tool_failures.append(db_result)
            else:
                db_confirmation_id = db_result
                logger.info(
                    "integration_node: DB write confirmed — invoice='%s', id=%s.",
                    invoice_id, db_confirmation_id,
                )

            if isinstance(crm_result, Exception):
                logger.error(
                    "integration_node: push_invoice_to_crm failed for '%s': %s",
                    invoice_id, crm_result,
                )
                tool_failures.append(crm_result)
            else:
                crm_record_id = crm_result
                logger.info(
                    "integration_node: CRM push confirmed — invoice='%s', id=%s.",
                    invoice_id, crm_record_id,
                )

            # ── Step 4: Persist the combined result ───────────────────────────
            # Write the row even on partial failure — NULL columns make the
            # partial success visible for auditing rather than silently dropping it.
            async with conn.transaction():
                result_row_id = await _insert_integration_result(
                    conn,
                    workflow_id,
                    invoice_id,
                    db_confirmation_id,
                    crm_record_id,
                )
                integration_result_ids.append(result_row_id)
                logger.info(
                    "integration_node: integration_results row written — "
                    "invoice='%s', result_row_id=%s.",
                    invoice_id,
                    result_row_id,
                )

    logger.info(
        "integration_node: complete — workflow_id=%s | results=%d | tool_failures=%d.",
        workflow_id,
        len(integration_result_ids),
        len(tool_failures),
    )

    # ── Step 5: Return state updates ─────────────────────────────────────────
    # Re-raise the first tool failure after all rows are written.
    # This lets error_handler.py retry the node while preserving the audit trail
    # of partial successes that were already committed to integration_results.
    if tool_failures:
        raise tool_failures[0]

    return {
        "integration_result_ids": integration_result_ids,
        "completed_task_ids": [task_id],
        "status": "integration_complete",
    }
