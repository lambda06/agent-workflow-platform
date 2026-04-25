"""
backend.agents.transform_agent

LangGraph node — Transform Agent.

Responsibility:
    Runs after extraction_node. Reads the extracted_data_ids from state,
    fetches each raw invoice row from Supabase, validates it, and routes
    each invoice to one of two destination tables:
        - transformed_invoices : invoices that passed all validation checks
        - transform_errors     : invoices that failed validation (soft failures)

Partial success pattern — why validation failures go to a separate table:
    In a multi-invoice workflow, one malformed invoice should NEVER block the
    rest of the batch. A hard raise on any validation error would:
        (a) roll back valid work already done for the batch, and
        (b) force the entire extraction → transform → integration chain to retry,
            re-fetching and re-processing invoices that already passed.

    Instead, each invoice is processed independently:
        - Validation failure  → written to `transform_errors` with a reason string.
                                The workflow continues. The integration agent simply
                                skips invoices with no corresponding transformed row.
        - DB error (network, auth, constraint) → RAISED immediately. This is an
                                infrastructure failure, not a data problem. Let
                                error_handler.py decide whether to retry.

    The distinction is: "we couldn't validate this data" (soft) vs.
    "we couldn't talk to the database" (hard).

DB connection strategy — shared psycopg3 pool:

    Connection usage in this agent:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                ...  # per-invoice transaction wrapping

Per-invoice transaction strategy:
    Each invoice's DB writes are wrapped in their own transaction block.
    A DB error on invoice N does not undo committed rows for invoices 1..N-1.
    psycopg3's conn.transaction() is used for explicit per-invoice commit control.

Required Supabase tables (run once before Phase 2):
    CREATE TABLE IF NOT EXISTS transformed_invoices (
        id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id       TEXT        NOT NULL,
        invoice_id        TEXT        NOT NULL,
        data              JSONB       NOT NULL,
        validation_status TEXT        NOT NULL DEFAULT 'valid',
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS transform_errors (
        id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id  TEXT        NOT NULL,
        invoice_id   TEXT        NOT NULL,
        error_reason TEXT        NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from backend.db.pool import get_pool
from backend.orchestration.state_manager import WorkflowState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Holds the outcome of a single invoice validation pass.

    Attributes:
        invoice_id: The invoice_id field from the invoice dict (for logging).
        is_valid:   True if all checks passed; False if any check failed.
        errors:     Human-readable list of every failed check. Empty when valid.
        invoice:    The original invoice dict, unchanged.
    """
    invoice_id: str
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    invoice: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = {"invoice_id", "vendor_name", "total_amount", "invoice_date"}

# Tolerance for floating-point rounding when comparing total_amount to line-item sum.
# Invoices with discrepancies larger than this are flagged as invalid.
_AMOUNT_TOLERANCE = 0.01


def _validate_invoice(invoice: dict) -> ValidationResult:
    """
    Run all validation checks against a single invoice dict.

    Checks performed (in order):
        1. Required field presence: invoice_id, vendor_name, total_amount, invoice_date.
        2. Arithmetic integrity: total_amount == sum(qty * unit_price) across line_items.
        3. Date ordering: due_date (if present) is after invoice_date.

    All failures are collected before returning — the result contains every
    problem found, not just the first one. This makes the error_reason stored
    in transform_errors useful for human review.

    Args:
        invoice: Raw invoice dict fetched from extracted_invoices.data JSONB column.

    Returns:
        ValidationResult with is_valid=True if all checks passed, otherwise
        is_valid=False with a populated errors list.
    """
    invoice_id = invoice.get("invoice_id", "<unknown>")
    errors: list[str] = []

    # ── Check 1: Required field presence ─────────────────────────────────────
    missing = _REQUIRED_FIELDS - invoice.keys()
    if missing:
        errors.append(f"Missing required field(s): {', '.join(sorted(missing))}.")

    # ── Check 2: Arithmetic integrity ─────────────────────────────────────────
    # Only run if total_amount is present — missing field is already caught above.
    if "total_amount" in invoice:
        line_items: list[dict] = invoice.get("line_items", [])
        if line_items:
            calculated_total = sum(
                item.get("quantity", 0) * item.get("unit_price", 0.0)
                for item in line_items
            )
            declared_total: float = invoice["total_amount"]
            discrepancy = abs(declared_total - calculated_total)

            if discrepancy > _AMOUNT_TOLERANCE:
                errors.append(
                    f"total_amount mismatch: declared {declared_total:.2f}, "
                    f"calculated {calculated_total:.2f} "
                    f"(discrepancy: {discrepancy:.2f})."
                )

    # ── Check 3: Date ordering ────────────────────────────────────────────────
    # Only run if both dates are present and parseable.
    invoice_date_str: Optional[str] = invoice.get("invoice_date")
    due_date_str: Optional[str] = invoice.get("due_date")

    if invoice_date_str and due_date_str:
        try:
            invoice_date = date.fromisoformat(invoice_date_str)
            due_date = date.fromisoformat(due_date_str)
            if due_date <= invoice_date:
                errors.append(
                    f"due_date ({due_date_str}) must be after invoice_date ({invoice_date_str})."
                )
        except ValueError as exc:
            errors.append(f"Unparseable date value: {exc}.")

    return ValidationResult(
        invoice_id=invoice_id,
        is_valid=len(errors) == 0,
        errors=errors,
        invoice=invoice,
    )


# ---------------------------------------------------------------------------
# DB write helpers
# ---------------------------------------------------------------------------

async def _insert_transformed(
    conn,
    workflow_id: str,
    result: ValidationResult,
) -> str:
    """
    Write a validated invoice to the transformed_invoices table.

    Args:
        conn:        Active psycopg3 async connection from the shared pool.
        workflow_id: UUID string of the parent workflow run.
        result:      Passing ValidationResult containing the clean invoice dict.

    Returns:
        UUID string of the newly inserted row.
    """
    cur = await conn.execute(
        """
        INSERT INTO transformed_invoices (workflow_id, invoice_id, data, validation_status)
        VALUES (%s, %s, %s::jsonb, 'valid')
        RETURNING id::text
        """,
        (workflow_id, result.invoice_id, json.dumps(result.invoice)),
    )
    row = await cur.fetchone()
    return row["id"]


async def _insert_error(
    conn,
    workflow_id: str,
    result: ValidationResult,
) -> str:
    """
    Write a failed invoice and its error reasons to the transform_errors table.

    Multiple errors are joined with a pipe separator for easy parsing
    by the notification agent or a human reviewer.

    Args:
        conn:        Active psycopg3 async connection from the shared pool.
        workflow_id: UUID string of the parent workflow run.
        result:      Failing ValidationResult with a populated errors list.

    Returns:
        UUID string of the newly inserted error row.
    """
    error_reason = " | ".join(result.errors)
    cur = await conn.execute(
        """
        INSERT INTO transform_errors (workflow_id, invoice_id, error_reason)
        VALUES (%s, %s, %s)
        RETURNING id::text
        """,
        (workflow_id, result.invoice_id, error_reason),
    )
    row = await cur.fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_transform_task(tasks: list[dict]) -> Optional[dict]:
    """
    Locate the TRANSFORM task in the current state task list.

    Args:
        tasks: Lightweight task metadata dicts from WorkflowState.

    Returns:
        First task dict with agent_type == "transform", or None.
    """
    for task in tasks:
        if task.get("agent_type") == "transform":
            return task
    return None


async def _fetch_invoice_row(conn, row_id: str) -> Optional[dict]:
    """
    Fetch a single extracted_invoices row and return its JSONB data field.

    Args:
        conn:   Active psycopg3 async connection from the shared pool.
        row_id: UUID string of the extracted_invoices row.

    Returns:
        Parsed invoice dict, or None if the row does not exist.
    """
    cur = await conn.execute(
        "SELECT data FROM extracted_invoices WHERE id = %s::uuid",
        (row_id,),
    )
    record = await cur.fetchone()
    if record is None:
        return None
    # psycopg3 returns JSONB columns as a Python dict directly —
    # no manual json.loads() needed (unlike asyncpg which returned strings).
    raw = record["data"]
    return raw if isinstance(raw, dict) else json.loads(raw)


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

async def transform_node(state: WorkflowState) -> dict:
    """
    LangGraph transform node — validates extracted invoices and routes them.

    Steps:
        1. Locate the TRANSFORM task in state["tasks"].
        2. For each ID in state["extracted_data_ids"], fetch the raw invoice
           from Supabase extracted_invoices using the shared psycopg3 pool.
        3. Validate each invoice (required fields, arithmetic, date ordering).
        4. Write valid invoices to transformed_invoices; write failures to
           transform_errors. Each invoice is processed independently (per-invoice
           transaction) so a DB error on one row does not undo others.
        5. Return row IDs split into transformed_data_ids and error_ids.

    Partial success:
        Validation failures are NEVER raised — they are soft-handled and written
        to transform_errors. The workflow continues with whatever invoices passed.
        Only true DB errors (connection failures, constraint violations) are raised
        so that error_handler.py can decide whether to retry.

    Reads from state:
        state["workflow_id"]        : Namespace for all DB rows.
        state["tasks"]              : Searched for the TRANSFORM task to mark done.
        state["extracted_data_ids"] : Row IDs to fetch and validate.

    Returns:
        {
            "transformed_data_ids": list[str]  — transformed_invoices row UUIDs (valid).
            "error_ids":            list[str]  — transform_errors row UUIDs (invalid).
            "completed_task_ids":   list[str]  — The transform task ID.
            "status":               str        — "transform_complete"
        }

    Raises:
        RuntimeError: If no TRANSFORM task is found in state["tasks"].
        psycopg.*:    Any DB infrastructure error — propagated for retry.
    """
    workflow_id: str = state["workflow_id"]
    extracted_ids: list[str] = state.get("extracted_data_ids", [])

    # ── Step 1: Find the transform task ──────────────────────────────────────
    transform_task = _find_transform_task(state.get("tasks", []))

    if transform_task is None:
        raise RuntimeError(
            f"transform_node: no TRANSFORM task found in state for "
            f"workflow_id={workflow_id}. Coordinator may not have planned one."
        )

    task_id: str = transform_task["id"]
    logger.info(
        "transform_node: starting — workflow_id=%s, task_id=%s, invoices_to_process=%d.",
        workflow_id,
        task_id,
        len(extracted_ids),
    )

    if not extracted_ids:
        logger.warning(
            "transform_node: extracted_data_ids is empty for workflow_id=%s. "
            "Nothing to transform.",
            workflow_id,
        )
        return {
            "transformed_data_ids": [],
            "error_ids": [],
            "completed_task_ids": [task_id],
            "status": "transform_complete",
        }

    # ── Steps 2–4: Fetch, validate, and persist each invoice ─────────────────
    pool = get_pool()
    transformed_ids: list[str] = []
    error_ids: list[str] = []

    async with pool.connection() as conn:
        for row_id in extracted_ids:

            # Fetch raw invoice from extracted_invoices
            invoice = await _fetch_invoice_row(conn, row_id)

            if invoice is None:
                # Row missing — treat as a data error, not an infrastructure error.
                # Log and continue; do not crash the node over a missing row.
                logger.warning(
                    "transform_node: extracted_invoices row '%s' not found — skipping.",
                    row_id,
                )
                continue

            # Validate the invoice
            result = _validate_invoice(invoice)

            # Each invoice gets its own transaction so that a DB error on this
            # invoice does not roll back successfully committed rows above.
            async with conn.transaction():
                if result.is_valid:
                    # ── Valid invoice → transformed_invoices ──────────────────
                    transformed_row_id = await _insert_transformed(conn, workflow_id, result)
                    transformed_ids.append(transformed_row_id)
                    logger.info(
                        "transform_node: invoice '%s' passed validation → "
                        "transformed_invoices row_id=%s.",
                        result.invoice_id,
                        transformed_row_id,
                    )
                else:
                    # ── Invalid invoice → transform_errors ────────────────────
                    # Log each failure reason for observability, then persist to DB.
                    # Do NOT raise — this is a data problem, not an infra problem.
                    logger.warning(
                        "transform_node: invoice '%s' failed validation: %s",
                        result.invoice_id,
                        " | ".join(result.errors),
                    )
                    error_row_id = await _insert_error(conn, workflow_id, result)
                    error_ids.append(error_row_id)
                    logger.info(
                        "transform_node: invoice '%s' written to transform_errors → "
                        "error_row_id=%s.",
                        result.invoice_id,
                        error_row_id,
                    )

    logger.info(
        "transform_node: complete — workflow_id=%s | valid=%d | errors=%d.",
        workflow_id,
        len(transformed_ids),
        len(error_ids),
    )

    # ── Step 5: Return state updates ─────────────────────────────────────────
    # All four lists use operator.add reducers in WorkflowState — LangGraph
    # appends these to whatever other nodes have already written.
    return {
        "transformed_data_ids": transformed_ids,
        "error_ids": error_ids,
        "completed_task_ids": [task_id],
        "status": "transform_complete",
    }
