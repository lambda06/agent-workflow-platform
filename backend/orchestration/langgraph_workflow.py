"""
backend.orchestration.langgraph_workflow

Assembles the LangGraph StateGraph that orchestrates the five-agent invoice
processing pipeline.

Pipeline topology (Phase 1):

    ┌─────────────────┐
    │  coordinator    │  Decomposes user request → task plan
    └────────┬────────┘
             │  _route_after_coordinator()
             │  (conditional — checks which agent types are present)
             ▼
    ┌─────────────────┐
    │  extraction     │  Fetches invoices → extracted_invoices (Supabase)
    └────────┬────────┘
             │
             ▼
    ┌─────────────────┐
    │  transform      │  Validates → transformed_invoices / transform_errors
    └────────┬────────┘
             │
             ▼
    ┌─────────────────┐
    │  integration    │  Pushes to DB + CRM concurrently → integration_results
    └────────┬────────┘
             │
             ▼
    ┌─────────────────┐
    │  notification   │  Sends Slack summary → notification_results
    └────────┬────────┘
             │
             ▼
            END

Design decisions:

Conditional routing after coordinator_node:
    The coordinator writes a list of task dicts — each with an `agent_type`. Rather
    than hardcoding an unconditional edge coordinator → extraction, a conditional
    edge inspects the planned task types so the graph can short-circuit cleanly if
    the LLM produces an incomplete plan (e.g. no extraction task planned). For Phase 1
    the expected path is always the full linear chain. This also makes adding
    concurrent branches (e.g. run extraction and another agent in parallel) trivial
    in Phase 3 — just update the router to return a list of node names.

Checkpointer injection via get_runnable_workflow():
    LangGraph's AsyncPostgresSaver (backed by Supabase PostgreSQL) enables mid-run
    persistence and resumability. The checkpointer is NOT imported here — instead,
    get_runnable_workflow() accepts an optional checkpointer argument passed in from
    the API layer (main.py / lifespan hook). This keeps the orchestration layer
    decoupled from database initialisation order and makes the graph unit-testable
    without a live DB connection (pass checkpointer=None in tests).

    Shared pool with agents:
        AsyncPostgresSaver accepts a psycopg3 AsyncConnectionPool directly via
        AsyncPostgresSaver(pool=...). In the FastAPI lifespan, the same pool
        opened by backend.db.pool.init_pool() is passed to both the agents (via
        get_pool()) and the checkpointer — one driver, one pool, zero conflicts.

Module-level `workflow`:
    A default compiled graph (no checkpointer) is exposed as `workflow` at module
    level for use in tests and scripts that don't need persistence. Production code
    should call get_runnable_workflow(checkpointer=<AsyncPostgresSaver instance>).

evaluator_agent (Phase 6):
    The evaluator node will be inserted between integration and notification once
    implemented. Its import and node wiring are left as a TODO comment below.
"""

import logging
from typing import Optional

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.orchestration.state_manager import WorkflowState, get_initial_state  # noqa: F401 — re-exported for API layer convenience
from backend.agents.coordinator_agent import coordinator_node
from backend.agents.extraction_agent import extraction_node
from backend.agents.transform_agent import transform_node
from backend.agents.integration_agent import integration_node
from backend.agents.notification_agent import notification_node

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conditional router — called after coordinator_node
# ---------------------------------------------------------------------------

# Canonical node names used as routing targets. Defining them as constants
# avoids silent typo bugs (a misspelled string in add_conditional_edges would
# create a disconnected branch rather than raising immediately).
_NODE_EXTRACTION    = "extraction"
_NODE_TRANSFORM     = "transform"
_NODE_INTEGRATION   = "integration"
_NODE_NOTIFICATION  = "notification"


def _route_after_coordinator(state: WorkflowState) -> str:
    """
    Inspect the planned task list and return the name of the next node to visit.

    Called by LangGraph as the conditional edge function after coordinator_node
    completes. In Phase 1 the pipeline is always linear (coordinator always plans
    an extraction task), so this always returns _NODE_EXTRACTION.

    The conditional structure (rather than a plain unconditional edge) is
    intentional:
        - It makes the graph robust to partial plans: if the coordinator omits an
          extraction task (e.g. the user request requires only notification), we can
          route accordingly instead of crashing inside extraction_node with
          "no EXTRACTION task found".
        - It is the natural extension point for adding parallel fan-out in Phase 3:
          returning a list of node names runs them concurrently.

    Args:
        state: The WorkflowState dict after coordinator_node has run.

    Returns:
        The name of the next node to execute, or END if the plan is empty.
    """
    tasks: list[dict] = state.get("tasks", [])

    if not tasks:
        logger.warning(
            "_route_after_coordinator: coordinator produced no tasks for "
            "workflow_id=%s — routing directly to END.",
            state.get("workflow_id", "unknown"),
        )
        return END  # type: ignore[return-value]

    # Check which agent types are present in the plan.
    planned_types = {t.get("agent_type") for t in tasks}

    logger.info(
        "_route_after_coordinator: planned agent types for workflow_id=%s: %s",
        state.get("workflow_id", "unknown"),
        planned_types,
    )

    # Phase 1: full pipeline — extraction is always the next step.
    # Future: branch on planned_types to support alternate entry points.
    if "extraction" in planned_types:
        return _NODE_EXTRACTION

    # Fallback: if no extraction task, jump straight to transform (future use).
    if "transform" in planned_types:
        return _NODE_TRANSFORM

    # Fallback: data already transformed, only need to integrate + notify.
    if "integration" in planned_types:
        return _NODE_INTEGRATION

    # Notification-only workflow (e.g. status ping).
    if "notification" in planned_types:
        return _NODE_NOTIFICATION

    # Unknown agent type plan — end gracefully rather than crashing.
    logger.error(
        "_route_after_coordinator: no recognised agent types in plan for "
        "workflow_id=%s. Routing to END.",
        state.get("workflow_id", "unknown"),
    )
    return END  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph() -> StateGraph:
    """
    Construct and return the un-compiled StateGraph.

    Separated from compilation so that get_runnable_workflow() can inject a
    checkpointer at compile time without rebuilding the graph structure.

    Returns:
        A fully wired (but un-compiled) StateGraph[WorkflowState].
    """
    graph = StateGraph(WorkflowState)

    # ── Node registration ──────────────────────────────────────────────────────
    #
    # Each string label becomes the node's name in tracing, logging, and the
    # LangGraph Studio visualiser. Keep them consistent with the _NODE_* constants
    # above so conditional edge targets always resolve correctly.

    graph.add_node("coordinator",   coordinator_node)
    graph.add_node(_NODE_EXTRACTION,  extraction_node)
    graph.add_node(_NODE_TRANSFORM,   transform_node)
    graph.add_node(_NODE_INTEGRATION, integration_node)
    graph.add_node(_NODE_NOTIFICATION, notification_node)

    # Phase 4: evaluator_agent will be added here once implemented.
    # graph.add_node("evaluator", evaluator_node)

    # ── Entry point ────────────────────────────────────────────────────────────
    graph.set_entry_point("coordinator")

    # ── Edges ──────────────────────────────────────────────────────────────────
    #
    # After coordinator: conditional routing based on which task types were planned.
    # The path_map explicitly lists every possible return value of the router so
    # LangGraph can validate the graph topology at compile time (it will raise
    # if a returned string has no corresponding node).
    graph.add_conditional_edges(
        "coordinator",
        _route_after_coordinator,
        {
            _NODE_EXTRACTION:   _NODE_EXTRACTION,
            _NODE_TRANSFORM:    _NODE_TRANSFORM,
            _NODE_INTEGRATION:  _NODE_INTEGRATION,
            _NODE_NOTIFICATION: _NODE_NOTIFICATION,
            END:                END,
        },
    )

    # Linear pipeline edges for the standard full path:
    # extraction → transform → integration → notification → END
    graph.add_edge(_NODE_EXTRACTION,  _NODE_TRANSFORM)
    graph.add_edge(_NODE_TRANSFORM,   _NODE_INTEGRATION)
    graph.add_edge(_NODE_INTEGRATION, _NODE_NOTIFICATION)

    # Phase 4: insert the evaluator between integration and notification.
    # Replace the integration → notification edge with:
    #   graph.add_edge(_NODE_INTEGRATION, "evaluator")
    #   graph.add_edge("evaluator",       _NODE_NOTIFICATION)

    graph.add_edge(_NODE_NOTIFICATION, END)

    return graph


# ---------------------------------------------------------------------------
# Public factory — recommended entry point for production code
# ---------------------------------------------------------------------------

def get_runnable_workflow(checkpointer: Optional[BaseCheckpointSaver] = None):
    """
    Build and compile the LangGraph workflow, optionally attaching a checkpointer.

    This is the recommended entry point for the API layer. Pass an initialised
    AsyncPostgresSaver (backed by Supabase) to enable mid-run state persistence
    and workflow resumability after crashes.

    Passing checkpointer=None (the default) compiles a stateless graph suitable
    for unit tests and CLI invocations that don't need persistence.

    Args:
        checkpointer: An optional LangGraph BaseCheckpointSaver instance.
                      Typically an AsyncPostgresSaver created in the FastAPI
                      lifespan hook and passed in at startup.

    Returns:
        A compiled LangGraph CompiledGraph ready for .ainvoke() / .astream().

    Example (in FastAPI lifespan):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from backend.db.pool import get_pool
        from backend.orchestration.langgraph_workflow import get_runnable_workflow

        # The shared psycopg3 pool (opened by init_pool() in the lifespan)
        # is passed to both the agents (via get_pool()) and the checkpointer.
        # One pool, one driver — no SSL conflicts between asyncpg and psycopg3.
        pool = get_pool()
        checkpointer = AsyncPostgresSaver(pool=pool)
        await checkpointer.setup()  # creates LangGraph checkpoint tables if absent
        app.state.workflow = get_runnable_workflow(checkpointer=checkpointer)
    """
    graph = _build_graph()
    compiled = graph.compile(checkpointer=checkpointer)
    logger.info(
        "get_runnable_workflow: graph compiled — checkpointer=%s.",
        type(checkpointer).__name__ if checkpointer else "None (stateless)",
    )
    return compiled


# ---------------------------------------------------------------------------
# Module-level default graph — stateless, for tests and scripts
# ---------------------------------------------------------------------------

#: A pre-compiled, checkpointer-free graph instance.
#: Import and call `workflow.ainvoke(state)` directly in tests or CLI scripts.
#: For production use, prefer `get_runnable_workflow(checkpointer=...)` instead.
workflow = get_runnable_workflow()
