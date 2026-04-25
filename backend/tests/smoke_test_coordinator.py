"""
backend/tests/smoke_test_coordinator.py

Standalone async smoke test for coordinator_node.

Run from the project root:
    python -m backend.tests.smoke_test_coordinator

This script does NOT use pytest — it is a quick manual verification that:
  1. coordinator_node can be called with a realistic user request.
  2. The LLM returns a valid task plan (no hallucinated agent types).
  3. All returned tasks are properly structured and present in WorkflowState format.
"""

import asyncio
import sys

from backend.agents.coordinator_agent import coordinator_node
from backend.models.workflow import AgentType
from backend.orchestration.state_manager import get_initial_state

# ── Test configuration ────────────────────────────────────────────────────────

WORKFLOW_ID = "smoke-test-001"

USER_REQUEST = (
    "Extract invoices from emails, validate the amounts, "
    "update the database, and notify the finance team on Slack"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_AGENT_TYPES = {at.value for at in AgentType}

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
DIM    = "\033[2m"


def _print_task(index: int, task: dict) -> None:
    """Pretty-print a single task dict returned by coordinator_node."""
    deps = task.get("dependencies", [])
    deps_display = ", ".join(deps) if deps else f"{DIM}(none){RESET}"

    print(f"  {CYAN}Task {index}{RESET}")
    print(f"    id          : {task.get('id', 'MISSING')}")
    print(f"    agent_type  : {YELLOW}{task.get('agent_type', 'MISSING')}{RESET}")
    print(f"    description : {task.get('description', 'MISSING')}")
    print(f"    status      : {task.get('status', 'MISSING')}")
    print(f"    dependencies: {deps_display}")
    print()


# ── Main smoke test ───────────────────────────────────────────────────────────

async def run_smoke_test() -> bool:
    """
    Execute coordinator_node with a realistic request and validate the output.

    Returns:
        True if all assertions pass, False on any failure.
    """
    passed = True

    print(f"\n{BOLD}{'─' * 60}{RESET}")
    print(f"{BOLD} Coordinator Node — Smoke Test{RESET}")
    print(f"{BOLD}{'─' * 60}{RESET}\n")

    # ── Step 1: Build initial state ───────────────────────────────────────────
    print(f"{DIM}[1/4] Building initial WorkflowState via get_initial_state()...{RESET}")
    state = get_initial_state(
        workflow_id=WORKFLOW_ID,
        user_request=USER_REQUEST,
    )
    print(f"      workflow_id  : {state['workflow_id']}")
    print(f"      status       : {state['status']}")
    print(f"      user_request : {state['user_request']}\n")

    # ── Step 2: Call coordinator_node ─────────────────────────────────────────
    print(f"{DIM}[2/4] Calling coordinator_node(state)...{RESET}")
    try:
        result = await coordinator_node(state)
    except Exception as exc:
        print(f"\n  {RED}✖ coordinator_node raised an unexpected exception:{RESET}")
        print(f"    {type(exc).__name__}: {exc}\n")
        return False

    print(f"      status returned : {YELLOW}{result.get('status')}{RESET}")
    print(f"      tasks returned  : {len(result.get('tasks', []))}\n")

    # ── Step 3: Print each task ───────────────────────────────────────────────
    tasks: list[dict] = result.get("tasks", [])

    print(f"{DIM}[3/4] Task breakdown:{RESET}\n")
    if not tasks:
        print(f"  {RED}✖ No tasks returned — coordinator_node produced an empty plan.{RESET}\n")
        passed = False
    else:
        for i, task in enumerate(tasks, start=1):
            _print_task(i, task)

    # ── Step 4: Assertions ────────────────────────────────────────────────────
    print(f"{DIM}[4/4] Running assertions...{RESET}\n")
    failures: list[str] = []

    # 4a — status must be "planning_complete"
    if result.get("status") != "planning_complete":
        failures.append(
            f"Expected status='planning_complete', got '{result.get('status')}'"
        )

    # 4b — at least one task must be returned
    if not tasks:
        failures.append("coordinator_node returned zero tasks.")

    # 4c — every agent_type must be a valid AgentType enum value
    for task in tasks:
        agent_type = task.get("agent_type", "")
        if agent_type not in VALID_AGENT_TYPES:
            failures.append(
                f"Task '{task.get('id')}' has invalid agent_type: '{agent_type}' "
                f"(valid: {VALID_AGENT_TYPES})"
            )

    # 4d — every task must have the required WorkflowState fields
    required_fields = {"id", "agent_type", "description", "status", "dependencies"}
    for task in tasks:
        missing = required_fields - task.keys()
        if missing:
            failures.append(
                f"Task '{task.get('id')}' is missing required fields: {missing}"
            )

    # 4e — dependency IDs must reference actual task IDs in the plan
    task_ids = {t["id"] for t in tasks}
    for task in tasks:
        for dep_id in task.get("dependencies", []):
            if dep_id not in task_ids:
                failures.append(
                    f"Task '{task.get('id')}' has dependency '{dep_id}' "
                    f"that is not a known task ID in this plan."
                )

    # ── Result ────────────────────────────────────────────────────────────────
    if failures:
        passed = False
        print(f"  {RED}Assertion failures:{RESET}")
        for f in failures:
            print(f"    {RED}✖ {f}{RESET}")
        print()
    else:
        print(f"  {GREEN}✔ All assertions passed.{RESET}\n")

    return passed


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    success = asyncio.run(run_smoke_test())

    print(f"{BOLD}{'─' * 60}{RESET}")
    if success:
        print(f"{BOLD}{GREEN} ✔  PASS{RESET}")
    else:
        print(f"{BOLD}{RED} ✖  FAIL{RESET}")
    print(f"{BOLD}{'─' * 60}{RESET}\n")

    sys.exit(0 if success else 1)
