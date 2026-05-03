# Agent Workflow Platform — Folder Structure

Production-ready folder structure for a multi-agent AI workflow engine using **FastAPI**, **LangGraph**, **Google Gemini**, **Supabase PostgreSQL**, **Upstash Redis**, and **Langfuse**.

---

## Root Structure

```
agent-workflow-platform/
├── backend/                      # Main application package
├── tests/                        # Test suite
├── run.py                        # Windows-safe uvicorn entry point (Phase 3)
├── requirements.txt              # Pinned production dependencies
├── .env.example                  # Template for required environment variables
└── .env                          # Local secrets (gitignored)
```

---

## `backend/` — Main Application

| Folder | Purpose |
|--------|---------|
| **`config/`** | Pydantic `BaseSettings` pulling all credentials from environment variables. Fails fast at startup if any required variable is missing. |
| **`models/`** | Pydantic v2 request/response schemas. Covers both the input side (workflow requests, tasks) and the runtime output side (execution logs, errors, results). |
| **`orchestration/`** | LangGraph state schema, graph definition, and centralized error handling + retry logic. The orchestration core that all agents share. |
| **`agents/`** | Specialized agent node implementations. Each agent handles one domain of work and writes artifacts to Supabase, pushing only row IDs back into state. |
| **`tools/`** | Mock tool implementations used by agents for external integrations (APIs, databases, email, notifications). Swapped for real clients in later phases. |
| **`main.py`** | FastAPI application entrypoint. Lifespan hook, SSE streaming endpoint, status endpoint, CORS middleware. *(Phase 3 ✅)* |
| **`verify_connections.py`** | Standalone diagnostic script. Exercises all four external services end-to-end (Supabase, Redis, Langfuse, Gemini) and exits `0` when all pass. |

---

## `backend/config/` — Configuration

| File | Purpose |
|------|---------|
| **`settings.py`** | Single `Settings` class (Pydantic `BaseSettings`) loaded from `.env`. Covers Gemini API key, Supabase DB URL, Upstash Redis REST URL + token, Langfuse keys + host, HubSpot access token, Gmail OAuth2 paths, Slack bot token + channel ID, and app environment. `extra="ignore"` prevents undocumented env vars from crashing startup. |

---

## `backend/models/` — Data Models

| File | Purpose |
|------|---------|
| **`workflow.py`** | Input-side schemas: `TaskStatus` enum, `AgentType` enum, `Task` model, `WorkflowRequest` model. Defines the structure of every incoming workflow and its sub-tasks. |
| **`execution.py`** | Runtime-side schemas: `ExecutionLog` for per-step agent traces, `ExecutionError` for caught failures with stack traces, `WorkflowResult` for the final aggregated output of a completed run. |

---

## `backend/orchestration/` — Orchestration Core

| File | Purpose |
|------|---------|
| **`state_manager.py`** | `WorkflowState` TypedDict — the single shared memory object threaded through every LangGraph node. Reference-only: agents store heavy data in Supabase and write back only row IDs. All accumulator list fields use `operator.add` reducers for safe parallel-branch merging. Includes `get_initial_state()` factory. |
| **`error_handler.py`** | `ErrorClassifier` (retryable vs non-retryable), `RetryConfig` dataclass (max retries, delay, backoff multiplier, jitter), and `execute_with_retry()` async function with exponential backoff + ±20% jitter. Optional `on_failure` async hook for Supabase error persistence. |
| **`langgraph_workflow.py`** | Full `StateGraph` definition — nodes, edges, conditional routing, and graph compilation with optional `AsyncPostgresSaver` checkpointer. *(Phase 3 ✅)* |

---

## `backend/db/` — Database Layer

| File | Purpose |
|------|---------|
| **`pool.py`** | Centralised `psycopg3` `AsyncConnectionPool` shared by all agents and the LangGraph checkpointer. Lifecycle: `init_pool()` / `close_pool()` called from the FastAPI lifespan; `get_pool()` called by agent nodes. *(Phase 3 ✅)* |

---

## `backend/agents/` — Agent Nodes

| File | Purpose |
|------|---------|
| **`coordinator_agent.py`** | Entry node. Interprets the user request, decomposes it into tasks, and routes to the appropriate specialist agents. *(Phase 2)* |
| **`extraction_agent.py`** | Extracts structured data from raw sources (documents, APIs, web). Writes extracted artifacts to Supabase, appends row IDs to `extracted_data_ids` in state. *(Phase 2)* |
| **`transform_agent.py`** | Cleans, structures, and reshapes extracted data. Writes transformed records to Supabase, appends row IDs to `transformed_data_ids` in state. *(Phase 2)* |
| **`integration_agent.py`** | Sends processed results to external systems (APIs, CRMs, databases). Writes integration payloads to Supabase, appends row IDs to `integration_result_ids` in state. *(Phase 2)* |
| **`notification_agent.py`** | Dispatches notifications (email, Slack, webhooks). Writes notification records to Supabase, appends row IDs to `notification_result_ids` in state. *(Phase 2)* |
| **`evaluator_agent.py`** | Scores workflow output quality. Writes evaluation results to Supabase and contributes to `final_summary` in state. *(Phase 6 📋)* |

---

## `backend/tools/` — Agent Tools

| File | Purpose |
|------|---------|
| **`mock_api_tools.py`** | Mock external API call tools used by the integration agent during development. |
| **`mock_database_tools.py`** | Mock database read/write tools for local testing without a live DB. |
| **`mock_email_tools.py`** | Mock email dispatch tools used by the notification agent. |
| **`mock_notification_tools.py`** | Mock webhook and push notification tools for local development. |

---

## `tests/` — Testing

| Folder | Purpose |
|--------|---------|
| **`unit/`** | Unit tests for individual models, state logic, and error handler behaviour. *(Phase 4+)* |
| **`integration/`** | Integration tests against real Supabase, Redis, and Langfuse (or mocks). *(Phase 4+)* |
| **`e2e/`** | End-to-end workflow tests through the full LangGraph graph. *(Phase 4+)* |
| **`fixtures/`** | Shared test data, mock workflow requests, and reusable factories. *(Phase 4+)* |

---

## Technology Mapping

| Tech | Primary Location |
|------|-----------------|
| **FastAPI** | `backend/main.py` *(Phase 3 ✅)* |
| **LangGraph** | `backend/orchestration/langgraph_workflow.py`, `backend/orchestration/state_manager.py` |
| **Google Gemini** | `backend/config/settings.py` → agent nodes via `langchain-google-genai` |
| **Supabase PostgreSQL** | All agent nodes (artifact persistence) · `backend/db/pool.py` · `backend/verify_connections.py` |
| **Upstash Redis** | Agent caching layer *(Phase 4+)* · `backend/verify_connections.py` |
| **Langfuse** | Agent observability spans *(Phase 4+)* · `backend/verify_connections.py` |
| **Pydantic v2** | `backend/models/`, `backend/config/settings.py` |
| **`httpx`** | Integration agent tools and external API calls |
