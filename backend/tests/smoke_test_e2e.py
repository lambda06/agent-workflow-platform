"""
backend/tests/smoke_test_e2e.py

End-to-end smoke test for the full five-agent LangGraph workflow pipeline.

Run from the project root:
    python -m backend.tests.smoke_test_e2e

Optional verbose logging (shows all agent and LangGraph debug output):
    python -m backend.tests.smoke_test_e2e --verbose

Prerequisites:
    - GEMINI_API_KEY must be set (coordinator_node calls the live Gemini API).
    - SUPABASE_DATABASE_URL must be set and the following tables must exist:
        extracted_invoices, transformed_invoices, transform_errors,
        integration_results, notification_results.
    - The mock tools (email, DB, CRM, Slack) are used for all external I/O —
      no real Gmail, CRM, or Slack connection is required.

What this test validates:
    ┌─────────────────────────────────────────────────────────────────┐
    │ Node              │ Expected outcome                            │
    ├─────────────────────────────────────────────────────────────────┤
    │ coordinator       │ Plans 4 tasks (one per agent type)          │
    │ extraction        │ Inserts 3 invoices → extracted_data_ids     │
    │ transform         │ 1 passes (INV-2024-001), 2 fail             │
    │                   │   INV-2024-002: amount mismatch             │
    │                   │   INV-2024-003: missing total_amount        │
    │ integration       │ Pushes 1 valid invoice to DB + CRM          │
    │ notification      │ Sends Slack summary, persists result        │
    │ final status      │ "workflow_complete"                         │
    └─────────────────────────────────────────────────────────────────┘

Assertion rationale:
    - extracted_data_ids == 3: one row per mock invoice (always 3 from the mock)
    - transformed_data_ids == 1: only INV-2024-001 passes all validation checks
    - error_ids == 2: INV-2024-002 (amount mismatch) + INV-2024-003 (missing field)
    - integration_result_ids == 1: only the 1 valid invoice is integrated
    - notification_result_ids == 1: always 1 (one Slack summary per run)

Note on error_ids ambiguity:
    WorkflowState.error_ids accumulates IDs from BOTH transform_errors rows
    (soft validation failures written by transform_node) AND ExecutionError rows
    written by the error handler. In a healthy run with no infrastructure errors,
    all error_ids originate from transform_node validation failures only.
    The assertion len(error_ids) == 2 is therefore safe to make here.
"""

# ── Disable LangSmith tracing ─────────────────────────────────────────────────
# Must be set BEFORE any langchain / langgraph module is imported.
# Those packages read these env vars at import time to decide whether to
# initialise the tracing client. Setting them after the first import is too late
# — the client thread is already started and will attempt to POST to LangSmith.
import os
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import asyncio
import logging
import sys
import uuid

# ── Colour codes (ANSI) ───────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
DIM    = "\033[2m"

# ── Logging setup ─────────────────────────────────────────────────────────────
# Configured before imports so any module-level loggers are captured from
# the start. --verbose enables DEBUG on the root logger; default is WARNING
# so noise from LangGraph internals is suppressed unless needed.
# NOTE: The DB connection pool (backend.db.pool) must be opened via init_pool()
# before workflow.ainvoke() is called — agents no longer manage their own pools.

_verbose = "--verbose" in sys.argv

logging.basicConfig(
    level=logging.DEBUG if _verbose else logging.WARNING,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger("smoke_test_e2e")

# ── Imports (after logging is configured) ────────────────────────────────────

from backend.orchestration.langgraph_workflow import workflow  # noqa: E402
from backend.orchestration.state_manager import get_initial_state  # noqa: E402
from backend.db.pool import init_pool, close_pool  # noqa: E402

# ── Test configuration ────────────────────────────────────────────────────────

USER_REQUEST = (
    "Extract invoices from emails, validate the amounts, "
    "update the database, and notify the finance team on Slack"
)

# LangGraph recursion guard — prevents runaway graph loops if a bug causes
# a node to re-queue itself. 10 steps is well above the 5 nodes in the pipeline.
_RECURSION_LIMIT = 10


# ── Helper printers ───────────────────────────────────────────────────────────

def _section(title: str) -> None:
    print(f"\n{BOLD}{'─' * 64}{RESET}")
    print(f"{BOLD} {title}{RESET}")
    print(f"{BOLD}{'─' * 64}{RESET}")


def _step(n: int, total: int, msg: str) -> None:
    print(f"\n{DIM}[{n}/{total}] {msg}{RESET}")


def _ok(msg: str) -> None:
    print(f"  {GREEN}✔ {msg}{RESET}")


def _fail(msg: str) -> None:
    print(f"  {RED}✖ {msg}{RESET}")


def _info(label: str, value: object) -> None:
    print(f"      {CYAN}{label:<30}{RESET}: {YELLOW}{value}{RESET}")


def _print_ids(label: str, ids: list[str]) -> None:
    """Pretty-print a list of Supabase row IDs, or '(none)' when empty."""
    if not ids:
        print(f"      {CYAN}{label:<30}{RESET}: {DIM}(none){RESET}")
        return
    print(f"      {CYAN}{label:<30}{RESET}:")
    for row_id in ids:
        print(f"          {DIM}• {row_id}{RESET}")


# ── Assertion helper ──────────────────────────────────────────────────────────

def _assert(
    failures: list[str],
    condition: bool,
    success_msg: str,
    failure_msg: str,
) -> None:
    """
    Evaluate a single assertion and accumulate the result.

    Unlike a bare `assert`, this never raises — all failures are collected so
    the test prints every problem in one run rather than stopping at the first.

    Args:
        failures:    Mutable list to append failure messages to.
        condition:   The boolean result of the check.
        success_msg: Printed with ✔ when condition is True.
        failure_msg: Appended to failures and printed with ✖ when False.
    """
    if condition:
        _ok(success_msg)
    else:
        _fail(failure_msg)
        failures.append(failure_msg)


# ── Main smoke test ───────────────────────────────────────────────────────────

async def run_smoke_test() -> bool:
    """
    Execute the full five-agent workflow and validate every output field.

    Returns:
        True if all assertions pass, False if any assertion fails or an
        unexpected exception is raised during graph execution.
    """
    _section("End-to-End Pipeline Smoke Test")
    failures: list[str] = []

    # ── Step 1: Build initial state ───────────────────────────────────────────
    TOTAL_STEPS = 5
    _step(1, TOTAL_STEPS, "Building initial WorkflowState …")

    workflow_id = str(uuid.uuid4())
    initial_state = get_initial_state(
        workflow_id=workflow_id,
        user_request=USER_REQUEST,
    )

    _info("workflow_id",   workflow_id)
    _info("status",        initial_state["status"])
    _info("user_request",  initial_state["user_request"][:60] + "…")

    # ── Step 2: Invoke the full workflow ──────────────────────────────────────
    _step(2, TOTAL_STEPS, "Invoking workflow.ainvoke(initial_state) …")
    print(f"  {DIM}(This calls the live Gemini API for the coordinator node.){RESET}\n")

    try:
        # Open the shared psycopg3 pool before invoking the workflow.
        # All four agent nodes call get_pool() internally — pool must be ready
        # before the first node runs. In production this is done by the FastAPI
        # lifespan hook; in this smoke test we initialise it explicitly.
        await init_pool()

        final_state = await workflow.ainvoke(
            initial_state,
            config={"recursion_limit": _RECURSION_LIMIT},
        )
    except Exception as exc:
        print(f"\n  {RED}✖ workflow.ainvoke() raised an unexpected exception:{RESET}")
        print(f"    {type(exc).__name__}: {exc}\n")
        logger.exception("workflow.ainvoke() failed")
        return False
    finally:
        # Always close the pool, even if the workflow raised.
        await close_pool()

    # ── Step 3: Print final state ─────────────────────────────────────────────
    _step(3, TOTAL_STEPS, "Final state snapshot:")

    tasks: list[dict]  = final_state.get("tasks", [])
    extracted_ids      = final_state.get("extracted_data_ids", [])
    transformed_ids    = final_state.get("transformed_data_ids", [])
    error_ids          = final_state.get("error_ids", [])
    integration_ids    = final_state.get("integration_result_ids", [])
    notification_ids   = final_state.get("notification_result_ids", [])
    final_summary      = final_state.get("final_summary", "")
    status             = final_state.get("status", "")
    completed_task_ids = final_state.get("completed_task_ids", [])
    failed_task_ids    = final_state.get("failed_task_ids", [])

    print()
    _info("status",                   status)
    _info("tasks planned",            len(tasks))
    _info("completed_task_ids",       len(completed_task_ids))
    _info("failed_task_ids",          len(failed_task_ids))
    print()
    _print_ids("extracted_data_ids",     extracted_ids)
    _print_ids("transformed_data_ids",   transformed_ids)
    _print_ids("error_ids",              error_ids)
    _print_ids("integration_result_ids", integration_ids)
    _print_ids("notification_result_ids", notification_ids)

    if final_summary:
        print(f"\n      {CYAN}{'final_summary':<30}{RESET}:\n")
        for line in final_summary.splitlines():
            print(f"          {DIM}{line}{RESET}")
    else:
        print(f"\n      {CYAN}{'final_summary':<30}{RESET}: {DIM}(empty){RESET}")

    # ── Step 4: Task breakdown ────────────────────────────────────────────────
    _step(4, TOTAL_STEPS, "Task breakdown:")
    print()
    if tasks:
        for i, task in enumerate(tasks, start=1):
            agent_type  = task.get("agent_type", "MISSING")
            description = task.get("description", "MISSING")
            task_status = task.get("status", "MISSING")
            task_id     = task.get("id", "MISSING")
            # Truncate long descriptions
            desc_display = (description[:80] + "…") if len(description) > 80 else description
            print(
                f"  {CYAN}Task {i}{RESET}  "
                f"{YELLOW}{agent_type:<14}{RESET}  "
                f"{DIM}{task_status:<12}{RESET}  "
                f"{desc_display}"
            )
            print(f"        {DIM}id: {task_id}{RESET}")
    else:
        _fail("No tasks returned — coordinator produced an empty plan.")

    # ── Step 5: Assertions ────────────────────────────────────────────────────
    _step(5, TOTAL_STEPS, "Running assertions …")
    print()

    # 5a — Terminal status
    _assert(
        failures,
        status == "workflow_complete",
        f"status == 'workflow_complete'  (got: '{status}')",
        f"Expected status='workflow_complete', got '{status}'",
    )

    # 5b — Coordinator planned at least 1 task
    _assert(
        failures,
        len(tasks) >= 1,
        f"coordinator planned {len(tasks)} task(s) (≥ 1 required)",
        f"coordinator_node returned zero tasks — cannot continue pipeline",
    )

    # 5c — Extraction: 3 invoices from the mock (always deterministic)
    _assert(
        failures,
        len(extracted_ids) == 3,
        f"len(extracted_data_ids) == 3  (got: {len(extracted_ids)})",
        f"Expected 3 extracted invoices, got {len(extracted_ids)}",
    )

    # 5d — Transform pass: only INV-2024-001 is valid
    _assert(
        failures,
        len(transformed_ids) == 1,
        f"len(transformed_data_ids) == 1  (got: {len(transformed_ids)})",
        f"Expected 1 transformed (valid) invoice, got {len(transformed_ids)}",
    )

    # 5e — Transform errors: INV-2024-002 (amount mismatch) + INV-2024-003 (missing field)
    _assert(
        failures,
        len(error_ids) == 2,
        f"len(error_ids) == 2  (got: {len(error_ids)})",
        f"Expected 2 validation error rows, got {len(error_ids)}",
    )

    # 5f — Integration: 1 valid invoice pushed to DB + CRM
    _assert(
        failures,
        len(integration_ids) == 1,
        f"len(integration_result_ids) == 1  (got: {len(integration_ids)})",
        f"Expected 1 integration result row, got {len(integration_ids)}",
    )

    # 5g — Notification: exactly 1 Slack summary dispatched
    _assert(
        failures,
        len(notification_ids) == 1,
        f"len(notification_result_ids) == 1  (got: {len(notification_ids)})",
        f"Expected 1 notification result row, got {len(notification_ids)}",
    )

    # 5h — No tasks should have failed (healthy run, all mock tools succeed)
    _assert(
        failures,
        len(failed_task_ids) == 0,
        f"failed_task_ids is empty (no agent node crashes)",
        f"Unexpected task failures — failed_task_ids: {failed_task_ids}",
    )

    return len(failures) == 0


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    success = asyncio.run(run_smoke_test())

    print(f"\n{BOLD}{'─' * 64}{RESET}")
    if success:
        print(f"{BOLD}{GREEN} ✔  PASS — all assertions satisfied.{RESET}")
    else:
        print(f"{BOLD}{RED} ✖  FAIL — one or more assertions failed (see above).{RESET}")
    print(f"{BOLD}{'─' * 64}{RESET}\n")

    sys.exit(0 if success else 1)
