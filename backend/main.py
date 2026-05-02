"""
backend.main
============

Production FastAPI application entrypoint for the Agent Workflow Platform.

Startup sequence (managed by the lifespan context manager):
    1. Open the shared psycopg3 connection pool (backend.db.pool.init_pool).
    2. Initialise AsyncPostgresSaver using that same pool — one driver, zero conflicts.
    3. Call checkpointer.setup() to create LangGraph checkpoint tables if absent.
    4. Compile the LangGraph workflow with the live checkpointer attached.
    5. Store the compiled graph on app.state so request handlers can reach it
       without importing a global — avoids circular imports and makes testing easier.

Why lifespan, not module-level globals?
    Heavy async resources (DB pools, compiled graphs) must be created *inside* a
    running event loop. Module-level code runs at import time, before uvicorn starts
    the loop. Using lifespan guarantees correct ordering and clean teardown.

Why app.state instead of a module-level variable?
    app.state is the FastAPI-idiomatic way to share per-process objects across
    requests. It survives hot-reload (module-level globals don't), is accessible
    in tests via app.state directly, and doesn't pollute the module namespace.
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from uuid import uuid4

os.environ["LANGCHAIN_TRACING_V2"] = "false"  # strictly suppress LangSmith noise

# NOTE (Windows): WindowsSelectorEventLoopPolicy must be set in run.py BEFORE
# uvicorn.run() is called — setting it here is too late because uvicorn creates
# its event loop before importing this module. See run.py at the project root.

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import psycopg
from psycopg.rows import dict_row

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from backend.config.settings import settings
from backend.db.pool import close_pool, get_pool, init_pool
from backend.orchestration.langgraph_workflow import get_runnable_workflow
from backend.orchestration.state_manager import get_initial_state

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — startup and shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager: owns the full lifecycle of all async
    resources. Everything before `yield` runs at startup; everything after
    runs at shutdown.

    Resource order matters:
        - Pool first: checkpointer and agents both depend on it.
        - Checkpointer second: needs a dedicated autocommit connection for
          setup() (see note below), then the pool for runtime.
        - Workflow last: compilation is synchronous but references the
          checkpointer, so it must come after setup() completes.

    Why two connections for the checkpointer?
        AsyncPostgresSaver.setup() runs LangGraph migrations, which include
        CREATE INDEX CONCURRENTLY. Postgres forbids this DDL inside a
        transaction block. Our shared pool uses autocommit=False (the psycopg3
        default), so every statement runs in an implicit transaction — causing
        setup() to fail.

        The fix mirrors exactly what AsyncPostgresSaver.from_conn_string() does
        internally: open a direct connection with autocommit=True, call setup()
        on it, then close it. The runtime checkpointer then uses the shared
        pool normally (autocommit=False is correct for queries and writes).
    """

    # ── Startup ──────────────────────────────────────────────────────────────

    # 1. Open the shared psycopg3 pool.
    #    All agents use get_pool() — this single call satisfies them all.
    logger.info("lifespan: opening database connection pool …")
    await init_pool()

    # 2. Run LangGraph migrations on a dedicated autocommit=True connection.
    #    CREATE INDEX CONCURRENTLY (used in migrations) cannot run inside a
    #    transaction block, so the pool's default autocommit=False won't work.
    logger.info("lifespan: running LangGraph checkpoint migrations …")
    async with await psycopg.AsyncConnection.connect(
        settings.supabase_database_url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as setup_conn:
        await AsyncPostgresSaver(setup_conn).setup()
    logger.info("lifespan: checkpoint tables verified/created.")

    # 3. Build the runtime checkpointer using the shared pool.
    #    The pool (autocommit=False) is correct for all read/write operations;
    #    only setup() needed the special autocommit=True connection above.
    logger.info("lifespan: initialising LangGraph checkpointer …")
    pool = get_pool()
    checkpointer = AsyncPostgresSaver(pool)

    # 4. Compile the workflow graph with persistence attached.
    #    graph.compile() is synchronous; storing on app.state makes it
    #    accessible in endpoint handlers via request.app.state.workflow.
    app.state.workflow = get_runnable_workflow(checkpointer=checkpointer)
    app.state.checkpointer = checkpointer  # stored separately for status queries
    logger.info("lifespan: workflow compiled and ready.")

    yield  # ← application is live and handling requests

    # ── Shutdown ─────────────────────────────────────────────────────────────

    # Close pool last — checkpointer holds no independent connection, so there
    # is nothing else to drain before the pool closes.
    logger.info("lifespan: closing database connection pool …")
    await close_pool()
    logger.info("lifespan: shutdown complete.")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agent Workflow Platform",
    description=(
        "Production multi-agent orchestration engine built on LangGraph + FastAPI. "
        "Orchestrates invoice extraction, transformation, CRM integration, and Slack "
        "notifications as a fully checkpointed async pipeline."
    ),
    version="0.3.0",  # Phase 3
    lifespan=lifespan,
)

# CORS — allow all origins for portfolio demo purposes.
# In production, replace ["*"] with an explicit allowlist of frontend origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class WorkflowRunRequest(BaseModel):
    """Request body for POST /workflow/run/stream."""

    user_request: str
    """The natural-language instruction to feed into the coordinator agent."""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["observability"])
async def health_check():
    """
    Lightweight liveness probe.

    Returns the environment name so load balancers / CD pipelines can verify
    they are hitting the correct deployment tier without touching the DB.
    """
    return {"status": "ok", "environment": settings.environment}


@app.post("/workflow/run/stream", tags=["workflow"])
async def run_workflow_stream(body: WorkflowRunRequest, request: Request):
    """
    Kick off a new workflow run and stream node-level progress as Server-Sent Events.

    Each SSE event carries the name of the node that just completed and the
    *delta* state it produced (stream_mode="updates" — only changed keys, not
    the full accumulated state). This keeps payloads compact and directly maps
    to the UI's expectation of incremental progress updates.

    The `thread_id` in the LangGraph config ties every checkpoint written during
    this run to `workflow_id`, making the run retrievable later via the GET
    /workflow/{workflow_id} endpoint.
    """
    workflow_id = str(uuid4())
    initial_state = get_initial_state(
        workflow_id=workflow_id,
        user_request=body.user_request,
    )

    # thread_id links every checkpoint written during this run to workflow_id.
    # Without it the checkpointer stores the run anonymously and GET
    # /workflow/{workflow_id} will never find it.
    langgraph_config = {"configurable": {"thread_id": workflow_id}}

    workflow = request.app.state.workflow

    async def event_generator():
        """
        Async generator that consumes the LangGraph astream iterator and yields
        correctly formatted SSE frames.

        stream_mode="updates":
            Emits {node_name: {changed_state_keys}} after each node completes.
            This is more efficient than the default "values" mode, which emits
            the entire accumulated state — potentially megabytes of data — after
            every single node.
        """
        try:
            async for chunk in workflow.astream(
                initial_state,
                langgraph_config,
                stream_mode="updates",
            ):
                # When stream_mode="updates", chunk is a dict: {"node_name": {state_updates}}
                for node_name, node_update in chunk.items():
                    payload = json.dumps({"event": node_name, "state": node_update})
                    yield f"data: {payload}\n\n"

        except Exception as exc:
            logger.exception(
                "run_workflow_stream: unhandled error in workflow_id=%s: %s",
                workflow_id,
                exc,
            )
            error_payload = json.dumps(
                {"event": "error", "detail": str(exc), "workflow_id": workflow_id}
            )
            yield f"data: {error_payload}\n\n"

        finally:
            # Always send the terminal event so the client can close the
            # EventSource connection cleanly rather than waiting for a timeout.
            final_payload = json.dumps(
                {"event": "workflow_complete", "status": "done", "workflow_id": workflow_id}
            )
            yield f"data: {final_payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Disable proxy/nginx buffering so events reach the client immediately.
            "X-Accel-Buffering": "no",
            # Allow the client to reconnect and resume from a known event ID in future.
            "Cache-Control": "no-cache",
        },
    )


@app.get("/workflow/{workflow_id}", tags=["workflow"])
async def get_workflow_status(workflow_id: str, request: Request):
    """
    Retrieve the current state of a workflow run by its ID.

    Implementation note — why the checkpointer, not a Supabase table?
        The Phase 5 persistence schema (Supabase `workflow_results` table +
        Alembic migrations) doesn't exist yet. The LangGraph AsyncPostgresSaver
        already stores the full latest state keyed by thread_id (= workflow_id)
        after every node — it is the authoritative source of truth until Phase 5
        creates a dedicated results table.

        Concretely: AsyncPostgresSaver.aget_tuple(config) returns a
        CheckpointTuple whose `.checkpoint["channel_values"]` field is the
        complete WorkflowState as of the last completed node.

    Returns:
        200 + full state dict  — workflow was found in the checkpointer.
        200 {"status": "pending"} — no checkpoint yet (run hasn't started or
            workflow_id is wrong but we don't want to 404 prematurely).
        404                     — explicit not-found for completely unknown IDs
            (distinguishable from "in_progress" by the 404 status code).
    """
    checkpointer: AsyncPostgresSaver = request.app.state.checkpointer
    config = {"configurable": {"thread_id": workflow_id}}

    try:
        checkpoint_tuple = await checkpointer.aget_tuple(config)
    except Exception as exc:
        logger.exception(
            "get_workflow_status: checkpointer query failed for workflow_id=%s: %s",
            workflow_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="Checkpointer query failed.") from exc

    if checkpoint_tuple is None:
        # No checkpoint found — could mean the run hasn't started yet or the
        # ID is invalid. Return 404 so the client can distinguish this from an
        # in-progress run (which would have at least one checkpoint).
        raise HTTPException(
            status_code=404,
            detail=f"No workflow found for id={workflow_id}. "
                   "The run may not have started yet or the ID is incorrect.",
        )

    # checkpoint_tuple.checkpoint["channel_values"] is the full latest state.
    latest_state = checkpoint_tuple.checkpoint.get("channel_values", {})

    return {
        "workflow_id": workflow_id,
        "status": latest_state.get("status", "unknown"),
        "final_summary": latest_state.get("final_summary", ""),
        "completed_task_ids": latest_state.get("completed_task_ids", []),
        "failed_task_ids": latest_state.get("failed_task_ids", []),
        "extracted_data_ids": latest_state.get("extracted_data_ids", []),
        "transformed_data_ids": latest_state.get("transformed_data_ids", []),
        "integration_result_ids": latest_state.get("integration_result_ids", []),
        "notification_result_ids": latest_state.get("notification_result_ids", []),
        "error_ids": latest_state.get("error_ids", []),
    }


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Run with: python -m backend.main
    # For hot-reload during development: uvicorn backend.main:app --reload
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.environment == "development",
        log_level="info",
    )
