"""
backend.agents.coordinator_agent

Entry point node for the LangGraph workflow graph.

Responsibility:
    Receives the user's plain-English request from WorkflowState and decomposes
    it into a structured, ordered list of Task objects written back to state.

Why with_structured_output:
    Gemini (and LLMs in general) will freely invent agent type names when prompted
    in free-form. A response like "agent_type": "scraper" or "agent_type": "formatter"
    is common and would silently create tasks no downstream node can handle.

    with_structured_output(CoordinatorOutput) forces the model to emit JSON that
    Pydantic immediately validates against our schema. AgentType is a str-enum with
    exactly 4 legal values (EXTRACTION, TRANSFORM, INTEGRATION, NOTIFICATION) —
    any other string fails validation before the dict ever touches WorkflowState.
    There is no prompt-engineering workaround that achieves the same guarantee.

UUID strategy for dependencies:
    Gemini cannot self-reference UUIDs it hasn't generated yet. Instead,
    CoordinatorTask uses symbolic string IDs (e.g. "task-1", "task-2"). After
    the LLM call, _resolve_tasks() maps symbolic IDs -> real UUIDs so that
    Task.dependencies contains proper UUID references compatible with the rest
    of the pipeline.
"""

import logging
import uuid
from typing import List, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field, ValidationError

from backend.config.settings import settings
from backend.models.workflow import AgentType, Task, TaskStatus
from backend.orchestration.state_manager import WorkflowState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM-facing schema (what Gemini fills in)
# ---------------------------------------------------------------------------

class CoordinatorTask(BaseModel):
    """
    Trimmed task schema used as structured output target for Gemini.

    Differences from the full Task model:
    - `id` is a plain short string (e.g. "task-1") — Gemini can self-reference
      these in the `dependencies` field. Real UUIDs are assigned after validation.
    - `dependencies` is List[str] for the same reason.
    - `status` and `created_at` are omitted — they are set by the coordinator
      after validation, not by the LLM.
    """

    id: str = Field(
        ...,
        description=(
            "Short symbolic identifier for this task, e.g. 'task-1'. "
            "Used only to express dependency ordering — real UUIDs are assigned after planning."
        ),
    )
    description: str = Field(
        ...,
        description="Clear, actionable instruction for the agent that will execute this task.",
    )
    agent_type: AgentType = Field(
        ...,
        description=(
            "The specialist agent responsible for this task. "
            "Must be exactly one of: extraction, transform, integration, notification."
        ),
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description=(
            "List of symbolic task IDs (from the `id` field above) that must complete "
            "before this task can start. Use an empty list if this task has no prerequisites."
        ),
    )


class CoordinatorOutput(BaseModel):
    """
    Top-level structured output schema that Gemini targets via with_structured_output.

    Wrapping tasks in a parent model (rather than returning a bare list) is required
    because with_structured_output expects a single root Pydantic object.
    """

    tasks: List[CoordinatorTask] = Field(
        ...,
        description="Ordered and dependency-annotated list of tasks that together fulfill the user request.",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# System prompt given to Gemini for task decomposition.
# Kept here (not in a separate file) for Phase 1 simplicity — move to a
# PromptRegistry / Langfuse prompt store in Phase 5 when A/B testing is needed.
_SYSTEM_PROMPT = """\
You are a workflow coordinator for an AI agent platform. Your job is to break down a user's
request into a minimal, ordered list of tasks that the platform's specialist agents can execute.

Available agent types and their responsibilities:
- extraction    : Fetch or extract raw data from a source (URLs, files, APIs, databases).
- transform     : Clean, restructure, or enrich already-extracted data.
- integration   : Send processed data to an external system (API write, CRM update, DB insert).
- notification  : Dispatch a notification to a human or system (email, Slack, webhook).

Rules:
1. Use only the four agent types listed above — no others.
2. Express task ordering through the `dependencies` field using the symbolic `id` values you assign.
3. Tasks with no prerequisites must have an empty `dependencies` list.
4. Keep descriptions concise but actionable — the executing agent reads them as instructions.
5. Produce the minimum number of tasks needed; do not over-decompose simple requests.
"""


def _resolve_tasks(coordinator_tasks: List[CoordinatorTask]) -> List[dict]:
    """
    Convert Gemini's symbolic task plan into the lightweight dict format expected
    by WorkflowState["tasks"], replacing symbolic IDs with real UUIDs.

    Process:
        1. Assign a real UUID to every CoordinatorTask.
        2. Build a mapping from symbolic_id -> real_uuid.
        3. Re-map each dependency list from symbolic IDs to real UUIDs.
        4. Return as list[dict] matching the WorkflowState task metadata schema:
               {"id": str, "agent_type": str, "description": str, "status": str}

    # Phase 1 only
    Note: `dependencies` is intentionally excluded from WorkflowState task dicts
    because the graph uses conditional edges for sequencing — the dependencies list
    is only needed during planning (here) and can be stored in Supabase if needed.

    Args:
        coordinator_tasks: Validated CoordinatorTask list from Gemini's response.

    Returns:
        List of lightweight task metadata dicts suitable for WorkflowState["tasks"].
    """
    # Step 1 & 2: assign real UUIDs, build symbolic -> real map
    symbolic_to_uuid: dict[str, str] = {}
    real_tasks: list[dict] = []

    for ct in coordinator_tasks:
        real_id = str(uuid.uuid4())
        symbolic_to_uuid[ct.id] = real_id
        real_tasks.append({
            "symbolic_id": ct.id,   # kept temporarily for dependency resolution below
            "id": real_id,
            "agent_type": ct.agent_type.value,
            "description": ct.description,
            "status": TaskStatus.PENDING.value,
            # Symbolic dependencies resolved in step 3
            "_dep_symbols": ct.dependencies,
        })

    # Step 3: resolve dependency symbols to real UUIDs and log any broken refs
    for task in real_tasks:
        resolved_deps = []
        for sym in task.pop("_dep_symbols"):
            if sym in symbolic_to_uuid:
                resolved_deps.append(symbolic_to_uuid[sym])
            else:
                logger.warning(
                    "coordinator_node: dependency '%s' in task '%s' has no matching task ID — skipping.",
                    sym,
                    task["symbolic_id"],
                )
        task["dependencies"] = resolved_deps
        # Remove temporary symbolic_id — not part of WorkflowState schema
        task.pop("symbolic_id")

    return real_tasks


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

async def coordinator_node(state: WorkflowState) -> dict:
    """
    LangGraph entry node — decomposes the user request into a structured task plan.

    Reads:
        state["user_request"]: The raw plain-English instruction from the user.

    Returns:
        {
            "tasks":  List[dict]  — lightweight task metadata appended to WorkflowState.tasks
                                    via the operator.add reducer.
            "status": str         — "planning_complete", signals the graph to advance.
        }

    Raises:
        ValidationError: If Gemini's output cannot be validated against CoordinatorOutput
                         (e.g. an unrecognised agent_type). The error propagates to the
                         graph's error handler node rather than silently corrupting state.
        ValueError:      If state["user_request"] is empty or missing.
    """
    user_request: str = state.get("user_request", "").strip()

    if not user_request:
        raise ValueError("coordinator_node: state['user_request'] is empty — cannot plan tasks.")

    logger.info(
        "coordinator_node: decomposing request for workflow_id=%s",
        state.get("workflow_id", "unknown"),
    )

    # ----------------------------------------------------------------
    # Build the LLM with structured output bound to CoordinatorOutput.
    #
    # with_structured_output(CoordinatorOutput) wraps the model so that:
    #   - The response is parsed and validated by Pydantic before returning.
    #   - Any agent_type value not in the AgentType enum raises ValidationError here,
    #     not later when an agent node tries to handle an unrecognised task type.
    #   - No manual JSON parsing or regex extraction is needed.
    # ----------------------------------------------------------------
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=settings.gemini_api_key,
        temperature=0,          # deterministic planning; creativity adds noise here
    )

    # Bind structured output — Gemini receives the JSON schema derived from
    # CoordinatorOutput and is constrained to emit conforming JSON only.
    structured_llm = llm.with_structured_output(CoordinatorOutput)

    # ----------------------------------------------------------------
    # Invoke the model
    # ----------------------------------------------------------------
    messages = [
        ("system", _SYSTEM_PROMPT),
        ("human", user_request),
    ]

    logger.info("coordinator_node: invoking Gemini for task decomposition.")

    # ValidationError is intentionally not caught here — it propagates to the
    # graph's error handler node so the failure is logged, persisted to Supabase,
    # and surfaced to the caller rather than silently returning an empty task list.
    coordinator_output: CoordinatorOutput = await structured_llm.ainvoke(messages)

    logger.info(
        "coordinator_node: Gemini returned %d task(s) for workflow_id=%s.",
        len(coordinator_output.tasks),
        state.get("workflow_id", "unknown"),
    )

    # ----------------------------------------------------------------
    # Resolve symbolic IDs -> real UUIDs and convert to WorkflowState format
    # ----------------------------------------------------------------
    resolved_tasks = _resolve_tasks(coordinator_output.tasks)

    logger.info(
        "coordinator_node: planning complete — %d task(s) written to state.",
        len(resolved_tasks),
    )

    # Return only the fields this node owns; LangGraph merges via reducers.
    # `tasks` uses operator.add — existing list is preserved, new tasks appended.
    return {
        "tasks": resolved_tasks,
        "status": "planning_complete",
    }
