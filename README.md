# 🤖 Agent Workflow Platform

> **Building in Public** — A production-grade multi-agent orchestration system built from scratch, documented phase by phase with real engineering decisions, bottlenecks, and fixes.

A production-ready **AI Agent Workflow Engine** built on FastAPI + LangGraph. The platform orchestrates autonomous multi-agent pipelines where specialized agents (extractor, transformer, executor, notifier, evaluator) collaborate on complex, multi-step tasks — persisting heavy artifacts in Supabase and keeping LangGraph state lean for fast checkpointing.

The most interesting part of this project? **The workflow engine can retry intelligently, classify errors deterministically, and checkpoint mid-run — so a failure in one agent doesn't kill the whole pipeline.**

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| **API** | FastAPI · Uvicorn · Pydantic v2 |
| **Agent Orchestration** | LangGraph · LangChain Core |
| **LLM** | Google Gemini (`gemma-3-27b-it`) · `langchain-google-genai` |
| **Database / Persistence** | Supabase PostgreSQL · SQLAlchemy async · `asyncpg` |
| **Caching / State** | Upstash Redis (serverless REST API) |
| **Observability** | Langfuse (tracing · spans · prompt versioning) |
| **HTTP Client** | `httpx` (async, used by integration agents) |
| **Config** | Pydantic `BaseSettings` · `python-dotenv` |

---

## 🗺 Build Roadmap

```
Phase 1 🔨  Foundation        — Project scaffold · Config · Models · Orchestration core
Phase 2 📋  Agent Nodes       — Coordinator · Extractor · Transformer · Executor · Notifier · Evaluator
Phase 3 📋  LangGraph Graph   — Full StateGraph · Conditional routing · Parallel branches
Phase 4 📋  API Layer         — FastAPI endpoints · Request validation · Auth middleware
Phase 5 📋  Persistence       — Supabase schema · Alembic migrations · Result storage
Phase 6 📋  Deployment        — Docker · CI/CD · Cloud
```

---

## 🏗 Phase 1 — Foundation ✅

**Goal:** Establish the project scaffold, wire all external services, define the canonical data models, and build the orchestration core (state schema + error handling) before writing a single agent node.

### What was built

- **Project Scaffold** (`backend/`) — Full directory hierarchy created upfront with placeholder files, each annotated with its future purpose. Directories: `agents/`, `config/`, `models/`, `orchestration/`, `tools/`.

- **Dependency Manifest** (`requirements.txt`) — Exact pinned versions for all 13 production dependencies: FastAPI, LangGraph, `langchain-google-genai`, Pydantic v2, SQLAlchemy async, `asyncpg`, Upstash Redis, Langfuse, and `httpx`. Each entry carries an inline comment explaining its role.

- **Config / Settings** (`backend/config/settings.py`) — Pydantic `BaseSettings` pulling all credentials from environment variables. Fails fast at startup if any required variable is missing. Covers: Gemini API key, Supabase DB URL, Upstash Redis REST URL + token, Langfuse keys + host, and app environment. `model_config` set to `extra="ignore"` so undocumented env vars don't crash startup.

- **Connection Verifier** (`backend/verify_connections.py`) — Standalone diagnostic script (`python -m backend.verify_connections`) that exercises all four external services end-to-end:
  - **Supabase PostgreSQL** — Async SQLAlchemy engine + `SELECT 1` query
  - **Upstash Redis** — Async `SET`/`GET`/`DEL` round-trip
  - **Langfuse** — `auth_check()` credential validation
  - **Google Gemini** — Live `invoke()` call via `ChatGoogleGenerativeAI`
  
  Exits `0` when all pass, `1` when any fail. LangSmith tracing disabled at module level to suppress 403 noise from LangChain's automatic tracing injection.

- **Workflow Models** (`backend/models/workflow.py`) — Pydantic v2 models for the input side of the pipeline:
  - `TaskStatus` (enum) — `pending | in_progress | completed | failed | cancelled`
  - `AgentType` (enum) — `EXTRACTION | TRANSFORM | INTEGRATION | NOTIFICATION | COORDINATOR`
  - `Task` — Unit of work with UUID id, description, agent type, status, dependency list, and UTC timestamp
  - `WorkflowRequest` — Top-level incoming request: UUID workflow ID, name, list of tasks, and creation timestamp

- **Execution Models** (`backend/models/execution.py`) — Pydantic v2 models for the output/runtime side:
  - `ExecutionLog` — Single agent action or state transition: log ID, task ID, timestamp, message, and arbitrary metadata dict
  - `ExecutionError` — Captured failure: error ID, task ID, timestamp, error type, message, and optional stack trace
  - `WorkflowResult` — Final aggregated outcome: result ID, parent workflow ID, status string, `final_output` dict, list of logs, list of errors, and completion timestamp

- **Workflow State** (`backend/orchestration/state_manager.py`) — `TypedDict` representing the LangGraph graph's shared memory. Designed *reference-only* — agents write heavy artifacts to Supabase and push back only row IDs into state, keeping the checkpoint payload small and fast.

  Fields:
  | Field | Type | Purpose |
  |---|---|---|
  | `workflow_id` | `str` | Namespace for all Supabase writes in this run |
  | `status` | `str` | Lifecycle phase: `initializing → running → completed / failed / partial` |
  | `user_request` | `str` | Raw user instruction that triggered the workflow |
  | `tasks` | `Annotated[list[dict], operator.add]` | Lightweight task metadata (id, agent_type, description, status) |
  | `completed_task_ids` | `Annotated[list[str], operator.add]` | UUIDs of tasks that finished successfully |
  | `failed_task_ids` | `Annotated[list[str], operator.add]` | UUIDs of tasks that failed |
  | `extracted_data_ids` | `Annotated[list[str], operator.add]` | Supabase row IDs for raw extracted artifacts |
  | `transformed_data_ids` | `Annotated[list[str], operator.add]` | Supabase row IDs for cleaned/structured records |
  | `integration_result_ids` | `Annotated[list[str], operator.add]` | Supabase row IDs for external integration payloads |
  | `notification_result_ids` | `Annotated[list[str], operator.add]` | Supabase row IDs for dispatched notifications |
  | `error_ids` | `Annotated[list[str], operator.add]` | Supabase row IDs for persisted `ExecutionError` rows |
  | `final_summary` | `str` | Human-readable output written once by the summarizer |

  All accumulator list fields use `operator.add` as their LangGraph reducer — parallel agent branches append to the list rather than overwriting each other.

  Also includes `get_initial_state(workflow_id, user_request)` factory returning a clean `WorkflowState` with `status="initializing"` and all lists empty.

- **Error Handler** (`backend/orchestration/error_handler.py`) — Production-grade retry and error classification module for all agent calls:
  - `RateLimitError`, `AuthenticationError`, `InvalidInputError` — sentinel exception types for deterministic classification
  - `ErrorClassifier.classify(exc)` — Returns `"retryable"` or `"non_retryable"`. Non-retryable checked first (fail fast); unknown types default to `"retryable"` (fail-safe for long workflows)
  - `RetryConfig` — Dataclass: `max_retries=2`, `base_delay=1.0`, `backoff_multiplier=2.0`, `jitter=True`. Includes `compute_delay(attempt)` encapsulating the backoff formula
  - `execute_with_retry(func, retry_config, task_id, agent_name, on_failure?)` — Async retry loop with structured per-attempt logging. Non-retryable errors abort immediately; retryable errors sleep with exponential backoff + ±20% jitter before retrying; optional `on_failure` async hook fires after exhaustion for Supabase error persistence without coupling retry logic to the DB layer

### 🐛 Issues Encountered & Resolutions

**Issue 1: `Could not find name 'false'` on line 11 of `verify_connections.py`**
- **Where:** `backend/verify_connections.py`
- **Cause:** `LANGCHAIN_TRACING_V2=false` was written as a bare assignment using Python syntax that looks like a shell export. `false` is not a Python builtin — it's `False` in Python or a shell primitive. The linter caught it as an unresolved name.
- **Fix:** Replaced with proper Python environment variable assignment:
  ```python
  import os
  os.environ["LANGCHAIN_TRACING_V2"] = "false"
  ```
  Note: setting this via `.env` or shell alone is insufficient — LangChain reads tracing flags at import time, so the assignment must happen in Python before any LangChain imports execute (same root cause as Phase 3 in the RAG platform).

**Issue 2: Accumulator list fields in `WorkflowState` typed as plain `list[str]`**
- **Where:** `backend/orchestration/state_manager.py`
- **Cause:** Plain `list[str]` on a TypedDict field gives LangGraph no merge strategy. When two parallel agent nodes write to the same field simultaneously, the second write silently overwrites the first — IDs are lost with no error or warning.
- **Fix:** All accumulator fields annotated with `Annotated[list[str], operator.add]`. LangGraph calls `operator.add(existing, new)` at merge time, concatenating both writes. This prevents ID loss, avoids graph deadlocks, and costs nothing at runtime.

---

## 📁 Project Structure

```
agent-workflow-platform/
├── backend/
│   ├── agents/                   # Agent node implementations (Phase 2)
│   │   ├── coordinator_agent.py  # Routes user request to task graph
│   │   ├── extraction_agent.py   # Extracts structured data from sources
│   │   ├── transform_agent.py    # Cleans and reshapes extracted data
│   │   ├── integration_agent.py  # Sends results to external APIs / DBs
│   │   ├── notification_agent.py # Dispatches notifications (email, webhook)
│   │   └── evaluator_agent.py    # Scores workflow output quality
│   ├── config/
│   │   └── settings.py           # Pydantic BaseSettings — all env config
│   ├── models/
│   │   ├── workflow.py           # WorkflowRequest, Task, TaskStatus, AgentType
│   │   └── execution.py          # ExecutionLog, ExecutionError, WorkflowResult
│   ├── orchestration/
│   │   ├── state_manager.py      # WorkflowState TypedDict + get_initial_state()
│   │   ├── error_handler.py      # ErrorClassifier, RetryConfig, execute_with_retry()
│   │   └── langgraph_workflow.py # LangGraph StateGraph definition (Phase 3)
│   ├── tools/                    # Mock tool implementations for agent use (Phase 2)
│   │   ├── mock_api_tools.py
│   │   ├── mock_database_tools.py
│   │   ├── mock_email_tools.py
│   │   └── mock_notification_tools.py
│   ├── main.py                   # FastAPI app entrypoint (Phase 4)
│   └── verify_connections.py     # Diagnostic script for all external services
├── tests/                        # Test suite (Phase 4+)
├── requirements.txt              # Pinned production dependencies
├── .env.example                  # Template for required environment variables
└── .env                          # Local secrets (gitignored)
```

---

## 🏗 Getting Started

### Prerequisites

- Python 3.12+
- Accounts: [Google AI Studio](https://aistudio.google.com) · [Supabase](https://supabase.com) · [Upstash](https://upstash.com) · [Langfuse](https://cloud.langfuse.com)

### Setup

1. Clone and create a virtual environment:
   ```bash
   git clone https://github.com/your-username/agent-workflow-platform.git
   cd agent-workflow-platform
   python -m venv .venv
   .\.venv\Scripts\activate     # Windows
   # source .venv/bin/activate  # macOS/Linux
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment:
   ```bash
   cp .env.example .env
   # Fill in GEMINI_API_KEY, SUPABASE_DATABASE_URL, UPSTASH_REDIS_*, LANGFUSE_* etc.
   ```

4. Verify all external services are reachable:
   ```bash
   python -m backend.verify_connections
   ```
   All four services should show ✔. If any fail, check your `.env` values before proceeding.

5. Start the API server (Phase 4+):
   ```bash
   uvicorn backend.main:app --reload
   ```

---

## 🔑 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | Google Gemini API key for LLM calls |
| `SUPABASE_DATABASE_URL` | ✅ | Async PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash Redis REST endpoint URL |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash Redis REST token |
| `LANGFUSE_PUBLIC_KEY` | ✅ | Langfuse public key for tracing |
| `LANGFUSE_SECRET_KEY` | ✅ | Langfuse secret key for tracing |
| `LANGFUSE_HOST` | ❌ | Langfuse instance URL (defaults to `https://cloud.langfuse.com`) |
| `ENVIRONMENT` | ❌ | App environment — `development`, `staging`, `production` (defaults to `development`) |

---

## 📄 License

[Add license information here]

---

*Follow along on LinkedIn as each phase ships — real engineering decisions, not just demos.*
