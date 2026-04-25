"""
backend.agents.notification_agent

LangGraph node — Notification Agent.

Responsibility:
    Final node in the workflow graph. Runs after integration_node. Reads
    counters directly from WorkflowState to build a human-readable summary
    of the entire workflow run, dispatches it via Slack, and persists the
    notification record to Supabase.

Why notification failures are non-fatal:
    By the time this node runs, all the real work is already done and
    durably committed to Supabase:
        - Invoices extracted → extracted_invoices
        - Validation results → transformed_invoices + transform_errors
        - Integration outcomes → integration_results

    A Slack API timeout or auth failure at this stage should NOT mark the
    workflow as failed — every invoice has been correctly processed. Instead,
    the notification failure is:
        1. Logged at ERROR level for ops alerting.
        2. Persisted to notification_results with slack_message_id = NULL
           so there is an auditable record of the attempted notification.
        3. The node still returns status="workflow_complete" so the graph
           can reach its terminal state cleanly.

    Only DB errors when writing to notification_results are re-raised —
    infrastructure failures are retryable; Slack outages are best-effort.

DB connection strategy — shared psycopg3 pool:
    Connection usage in this agent:
        pool = get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                ...  # notification_results insert

Required Supabase table (run once before Phase 2):
    CREATE TABLE IF NOT EXISTS notification_results (
        id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        workflow_id      TEXT        NOT NULL,
        message          TEXT        NOT NULL,
        slack_message_id TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    );
"""

import logging
from typing import Optional

from backend.db.pool import get_pool
from backend.orchestration.state_manager import WorkflowState
from backend.tools.mock_notification_tools import send_slack_notification

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_notification_task(tasks: list[dict]) -> Optional[dict]:
    """
    Locate the NOTIFICATION task in the current state task list.

    Args:
        tasks: Lightweight task metadata dicts from WorkflowState.

    Returns:
        First task dict with agent_type == "notification", or None.
    """
    for task in tasks:
        if task.get("agent_type") == "notification":
            return task
    return None


def _build_summary(state: WorkflowState) -> str:
    """
    Build a human-readable workflow summary from WorkflowState counters.

    Reads the four accumulator lists directly from state — no DB query
    needed since the IDs are already in memory. List length gives the count.

    Args:
        state: Current WorkflowState at the end of the pipeline.

    Returns:
        Multi-line plain-text summary suitable for a Slack message.
    """
    extracted   = len(state.get("extracted_data_ids", []))
    validated   = len(state.get("transformed_data_ids", []))
    failed      = len(state.get("error_ids", []))
    integrated  = len(state.get("integration_result_ids", []))
    workflow_id = state.get("workflow_id", "unknown")
    user_req    = state.get("user_request", "")

    # Truncate long requests so the Slack message stays readable.
    preview = (user_req[:120] + "...") if len(user_req) > 120 else user_req

    lines = [
        f"*Workflow Complete* — `{workflow_id}`",
        f"*Request:* {preview}",
        "",
        "*Pipeline Summary:*",
        f"  • Invoices extracted   : {extracted}",
        f"  • Passed validation    : {validated}",
        f"  • Failed validation    : {failed}",
        f"  • Successfully integrated : {integrated}",
    ]

    # Add a brief outcome line so the reader can tell at a glance if
    # something needs follow-up without reading the full numbers.
    if failed > 0:
        lines.append(
            f"\n:warning: {failed} invoice(s) failed validation — "
            "check `transform_errors` table for details."
        )
    if integrated < validated:
        lines.append(
            f":warning: Only {integrated}/{validated} validated invoice(s) "
            "were successfully integrated."
        )
    if failed == 0 and integrated == validated:
        lines.append("\n:white_check_mark: All invoices processed successfully.")

    return "\n".join(lines)


async def _insert_notification_result(
    conn,
    workflow_id: str,
    message: str,
    slack_message_id: Optional[str],
) -> str:
    """
    Persist the notification record to notification_results.

    slack_message_id is nullable — NULL indicates the Slack call failed but
    the notification attempt was still recorded for auditing.

    Args:
        conn:             Active psycopg3 async connection from the shared pool.
        workflow_id:      UUID string of the parent workflow run.
        message:          The exact message text that was (attempted to be) sent.
        slack_message_id: Return value of send_slack_notification(), or None on failure.

    Returns:
        UUID string of the newly inserted row.
    """
    cur = await conn.execute(
        """
        INSERT INTO notification_results (workflow_id, message, slack_message_id)
        VALUES (%s, %s, %s)
        RETURNING id::text
        """,
        (workflow_id, message, slack_message_id),
    )
    row = await cur.fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

async def notification_node(state: WorkflowState) -> dict:
    """
    LangGraph notification node — sends workflow summary to Slack and persists result.

    Steps:
        1. Locate the NOTIFICATION task in state["tasks"].
        2. Build a human-readable summary from state counters (no DB reads needed).
        3. Call send_slack_notification() with the summary.
           On failure: log the error, set slack_message_id = None. Do NOT raise.
        4. Persist the notification record to notification_results using the
           shared psycopg3 pool (backend.db.pool.get_pool()).
        5. Return the result row ID and mark the workflow complete.

    Reads from state:
        state["workflow_id"]             : Used for DB namespacing and the message.
        state["user_request"]            : Shown as a preview in the Slack message.
        state["tasks"]                   : Searched for the NOTIFICATION task.
        state["extracted_data_ids"]      : Length used for "invoices extracted" count.
        state["transformed_data_ids"]    : Length used for "passed validation" count.
        state["error_ids"]               : Length used for "failed validation" count.
        state["integration_result_ids"]  : Length used for "integrated" count.

    Returns:
        {
            "notification_result_ids": list[str]  — notification_results row UUID.
            "completed_task_ids":      list[str]  — The notification task ID.
            "status":                  str        — "workflow_complete"
        }

    Raises:
        RuntimeError: If no NOTIFICATION task is found in state["tasks"].
        psycopg.*:    If the DB write to notification_results fails — this
                      is an infrastructure failure worth retrying.
    """
    workflow_id: str = state["workflow_id"]

    # ── Step 1: Find the notification task ───────────────────────────────────
    notification_task = _find_notification_task(state.get("tasks", []))

    if notification_task is None:
        raise RuntimeError(
            f"notification_node: no NOTIFICATION task found in state for "
            f"workflow_id={workflow_id}. Coordinator may not have planned one."
        )

    task_id: str = notification_task["id"]
    logger.info(
        "notification_node: starting — workflow_id=%s, task_id=%s.",
        workflow_id,
        task_id,
    )

    # ── Step 2: Build the summary message from state counters ─────────────────
    # State already holds all the counts as list lengths — no DB round-trip needed.
    message = _build_summary(state)
    logger.info("notification_node: summary message built (%d chars).", len(message))

    # ── Step 3: Send Slack notification (non-fatal on failure) ────────────────
    slack_message_id: Optional[str] = None
    try:
        slack_message_id = await send_slack_notification(message)
        logger.info(
            "notification_node: Slack notification sent — slack_message_id=%s.",
            slack_message_id,
        )
    except Exception as exc:
        # Non-fatal: log the failure and continue. The workflow is complete;
        # a Slack outage should not mark it as failed in the graph.
        logger.error(
            "notification_node: send_slack_notification() failed for "
            "workflow_id=%s: %s — recording NULL slack_message_id.",
            workflow_id,
            exc,
        )

    # ── Step 4: Persist the notification record ───────────────────────────────
    # DB write IS fatal — if Supabase is unreachable here, we want error_handler
    # to retry so the notification_results audit trail is complete.
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            result_row_id = await _insert_notification_result(
                conn, workflow_id, message, slack_message_id
            )

    logger.info(
        "notification_node: notification_results row written — "
        "workflow_id=%s, row_id=%s.",
        workflow_id,
        result_row_id,
    )

    # ── Step 5: Return state updates ─────────────────────────────────────────
    return {
        "notification_result_ids": [result_row_id],
        "completed_task_ids": [task_id],
        "status": "workflow_complete",
    }
