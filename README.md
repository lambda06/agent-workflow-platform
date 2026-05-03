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
| **Database / Persistence** | Supabase PostgreSQL · `psycopg[binary]` / `psycopg-pool` (Single Shared Pool) |
| **Caching / State** | Upstash Redis (serverless REST API) |
| **Observability** | Langfuse (tracing · spans · prompt versioning) |
| **HTTP Client** | `httpx` (async, used by integration agents) |
| **Config** | Pydantic `BaseSettings` · `python-dotenv` |

---

## 🗺 Build Roadmap

```
Phase 1 ✅  Foundation        — Scaffold · Config · Models · Orchestration core
Phase 2 ✅  Agent Nodes       — All 5 agents · StateGraph · Conditional routing · DB pool
Phase 3 ✅  API Layer         — FastAPI · SSE streaming · Status endpoint · Checkpointing
Phase 4 ✅  Real Integrations  — Gmail OAuth2 · HubSpot CRM · Supabase DB · Slack (all mocks replaced)
Phase 5 📋  Persistence       — Alembic migrations · Supabase schema · Results table
Phase 6 📋  Evaluator Agent   — LLM-as-judge · Confidence scoring · Auto-correct/escalate
Phase 7 📋  Deployment        — Docker · Dockerfile · docker-compose · Render · CI/CD · Auth middleware
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

## 🏗 Phase 2 — Agent Nodes & Orchestration ✅

**Goal:** Implement the specialized agent workers, define the LangGraph execution flow, build mock tools for isolated testing, and validate the pipeline end-to-end. Focus explicitly on deterministic JSON generation, robust state persistence, and concurrent tool execution.

### What was built

- **Coordinator Agent** (`backend/agents/coordinator_agent.py`) — The workflow's planner segment. Uses Gemini's `with_structured_output` API tightly bound to a dynamic Pydantic schema to decompose a natural language request into a sequence of dependent tasks. Operates securely by writing its planned execution path directly into the `WorkflowState`.

- **Action Agents** (`backend/agents/*.py`) — Highly constrained, specialized action nodes executing within the graph:
  - **Extraction Agent:** Fetches data (e.g. invoices) using external tools and persists the raw JSON payloads into Supabase.
  - **Transform Agent:** Applies strict structural/mathematical validations asynchronously across fetched data. Utilizes an independent, per-invoice transaction strategy ensuring malformed entries are routed to an errors table rather than failing the whole batch.
  - **Integration Agent:** Submits transformed data to multiple external sources utilizing `asyncio.gather(return_exceptions=True)` to maximize throughput during concurrent tool executions.
  - **Notification Agent:** End-of-the-line node. Interrogates runtime accumulators from internal state to formulate human-readable, deterministic summaries directly to Slack before sealing the graph completion.

- **Mock Tool Ecosystem** (`backend/tools/*.py`) — Local stubs substituting enterprise software (e.g., Salesforce CPQ, Gmail APIs, Slack webhooks). Returns hardcoded datasets containing specific edge cases (e.g., invalid arithmetic, missing fields) rigorously engineered to stress-test the Transform logic.

- **LangGraph Compilation Flow** (`backend/orchestration/langgraph_workflow.py`) — Connects the distinct agent actions. Injects conditional edge routing post-coordinator to map the LLM's dynamic task list stringently against physical graph nodes. Incorporates an optional `AsyncPostgresSaver` checkpointer for state resilience across executions.

- **End-to-End Smoke Test** (`backend/tests/smoke_test_e2e.py`) — Instantiates an isolated workflow run locally. Iterates over the running `astream` chunking mechanism and performs seven critical system assertions validating successful progression across every independent node, returning a clear `0` on success.

### 🐛 Issues Encountered & Resolutions

**Issue 1: Database Connection Proliferation & Driver Conflict (Crucial Architectural Change)**
- **Where:** Across all agent nodes, `backend/db/pool.py`, and `backend/main.py`.
- **Cause:** Initial plans had every agent establishing an isolated, lazy-loaded `asyncpg` pool (`min_size=2`). Parallel to this, LangGraph's `AsyncPostgresSaver` inherently utilizes `psycopg3`. Combining two varying wire-protocol drivers in tandem accessing a low-connection-limit Supabase database caused immediate exhaustion and fatal SSL negotiation conflicts.
- **Fix:** Jettisoned `asyncpg` and unused `sqlalchemy[asyncio]` dependencies across the entire project. Re-tooled to use a single, unified `AsyncConnectionPool` instantiated exclusively by `psycopg-pool`. Wired this global pool at FastAPIs startup `lifespan` hook directly into both agent DB handlers and LangGraphs native checkpointer—eliminating dual-driver overhead and connection sprawl gracefully.

---

## 🏗 Phase 3 — API Layer ✅

**Goal:** Expose the LangGraph workflow engine over HTTP. Build a production-grade FastAPI application with Server-Sent Events streaming, a checkpointer-backed status endpoint, a clean lifespan resource manager, and a Windows-safe custom entry point that forces `SelectorEventLoop` so `psycopg3` works reliably.

### What was built

- **Connection Pool Module** (`backend/db/pool.py`) — Centralised `psycopg3` `AsyncConnectionPool` accessible to every agent and the LangGraph checkpointer through three lifecycle functions:
  - `init_pool()` — Opens the pool at startup. Reads `POOL_MIN_SIZE` (default `2`) and `POOL_MAX_SIZE` (default `10`) from env vars. Uses `open=False` then `await _pool.open()` to avoid blocking the event loop during import.
  - `close_pool()` — Gracefully drains in-flight queries then closes all connections at shutdown.
  - `get_pool()` — Returns the live pool to callers. Raises `RuntimeError` immediately if called before `init_pool()` — surfaces wiring bugs early instead of producing confusing `NoneType` errors mid-request.

- **FastAPI Application** (`backend/main.py`) — Production entrypoint for the workflow engine:
  - **Lifespan context manager** (`@asynccontextmanager lifespan`) — Owns the full async resource lifecycle in strict dependency order:
    1. `init_pool()` — shared `psycopg3` pool opened first; all downstream resources depend on it.
    2. Dedicated `autocommit=True` connection → `AsyncPostgresSaver(setup_conn).setup()` — runs LangGraph Postgres migrations. `CREATE INDEX CONCURRENTLY` (used internally by LangGraph) is forbidden inside a transaction block; the shared pool uses `autocommit=False`, so a one-shot direct connection is required for migration only.
    3. `AsyncPostgresSaver(pool)` — runtime checkpointer wired to the shared pool for all subsequent reads and writes.
    4. `get_runnable_workflow(checkpointer=checkpointer)` → stored on `app.state.workflow` and `app.state.checkpointer`. Using `app.state` (not module-level globals) ensures objects survive hot-reload and avoids circular imports.
    5. `close_pool()` at shutdown — checkpointer holds no independent connection, so the pool is safe to drain last.

  - **CORS middleware** — `allow_origins=["*"]` for portfolio demo; comment in the code calls out the production change needed (explicit origin allowlist).

- **`GET /health`** — Lightweight liveness probe returning `{"status": "ok", "environment": "..."}`. Environment name lets CD pipelines confirm the correct deployment tier without hitting the database.

- **`POST /workflow/run/stream`** — Kicks off a new workflow run and streams live progress as Server-Sent Events:
  - Generates a fresh `uuid4()` as `workflow_id`; stores it as `thread_id` in the LangGraph config so every checkpoint written during the run is retrievable later.
  - Calls `workflow.astream(initial_state, config, stream_mode="updates")` — `stream_mode="updates"` emits only the changed state keys after each node (not the full accumulated state), keeping SSE payloads compact.
  - Each event is a JSON-encoded `{"event": "<node_name>", "state": {<delta>}}` frame sent as `data: ...\n\n`.
  - On any exception: emits `{"event": "error", "detail": "...", "workflow_id": "..."}` then falls through to the `finally` block.
  - `finally` always emits `{"event": "workflow_complete", "status": "done", "workflow_id": "..."}` so the client can cleanly close the `EventSource` without waiting for a timeout.
  - `StreamingResponse` headers set `X-Accel-Buffering: no` (disables nginx/proxy buffering) and `Cache-Control: no-cache`.

- **`GET /workflow/{workflow_id}`** — Retrieves the current state of a completed or in-progress run via the checkpointer:
  - Calls `checkpointer.aget_tuple({"configurable": {"thread_id": workflow_id}})` — the `AsyncPostgresSaver` is the authoritative state store until the Phase 5 Supabase results table exists.
  - Returns `404` for completely unknown IDs; a `200 {"status": "pending"}` semantics distinguishes "not started yet" from "unknown" at the client level.
  - Unpacks `checkpoint_tuple.checkpoint["channel_values"]` and returns a flat dict of all accumulator fields (`completed_task_ids`, `failed_task_ids`, `extracted_data_ids`, etc.).

- **Windows-safe Entry Point** (`run.py`) — A custom `uvicorn` bootstrap that forces `SelectorEventLoop` on Windows:
  - **Root cause:** `uvicorn` 0.44+ creates its event loop via an explicit `loop_factory` parameter, bypassing `asyncio`'s event loop policy entirely. The default Windows factory returns `ProactorEventLoop`, which `psycopg3`'s async pool cannot use.
  - **Why `set_event_loop_policy()` doesn't work:** `uvicorn` never calls `asyncio.new_event_loop()` — it calls the factory directly. No policy override can intercept this.
  - **Fix:** Drop one level below `uvicorn.run()` and call `uvicorn._compat.asyncio_run(server.serve(), loop_factory=asyncio.SelectorEventLoop)` directly — the same internal call `server.run()` makes, but with the correct factory injected. On Linux/macOS `loop_factory=None` falls through to uvicorn's default (also `SelectorEventLoop`).
  - Supports `--reload`, `--host`, and `--port` CLI flags via `argparse`.

### 🐛 Issues Encountered & Resolutions

**Issue 1: `CREATE INDEX CONCURRENTLY` fails inside transaction — `AsyncPostgresSaver.setup()` crashes at startup**
- **Where:** `backend/main.py` lifespan, `AsyncPostgresSaver(pool).setup()` call.
- **Cause:** LangGraph's migration SQL includes `CREATE INDEX CONCURRENTLY`, which Postgres prohibits inside an implicit transaction block. The shared `AsyncConnectionPool` uses `autocommit=False` (the `psycopg3` default), so every statement executes inside a transaction — causing `setup()` to fail with `ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block`.
- **Fix:** Mirror what `AsyncPostgresSaver.from_conn_string()` does internally: open a one-shot `AsyncConnection` with `autocommit=True` and `prepare_threshold=0`, call `setup()` on it, close it, then build the *runtime* checkpointer from the shared pool (which uses `autocommit=False` correctly for all read/write operations):
  ```python
  async with await psycopg.AsyncConnection.connect(
      settings.supabase_database_url,
      autocommit=True,
      prepare_threshold=0,
      row_factory=dict_row,
  ) as setup_conn:
      await AsyncPostgresSaver(setup_conn).setup()
  ```

**Issue 2: `psycopg3` pool crashes on Windows — `ProactorEventLoop` incompatibility**
- **Where:** Application startup on Windows when using `uvicorn backend.main:app --reload`.
- **Cause:** `uvicorn` 0.44+ bypasses `asyncio`'s event loop policy when creating its event loop, hardcoding `ProactorEventLoop` on Windows via a `loop_factory`. `psycopg3`'s `AsyncConnectionPool` requires `SelectorEventLoop`. `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` has no effect because it is never consulted.
- **Fix:** Replaced `uvicorn.run()` with a direct call to `uvicorn._compat.asyncio_run()` in a custom `run.py`, passing `loop_factory=asyncio.SelectorEventLoop` on Windows and `loop_factory=None` everywhere else. The server is now launched via `python run.py` instead of the `uvicorn` CLI.

**Issue 3: Streaming unpack error — `chunk.items()` called on non-dict**
- **Where:** `event_generator()` inside `POST /workflow/run/stream`.
- **Cause:** When `stream_mode="updates"`, `astream` emits `{node_name: {state_delta}}` dicts. Earlier iterations of the endpoint destructured the chunk assuming a tuple `(node_name, update)`, which raised `ValueError: too many values to unpack` on multi-key chunks.
- **Fix:** Switched to iterating `chunk.items()` explicitly, making the loop correct for any number of updated keys per chunk:
  ```python
  for node_name, node_update in chunk.items():
      payload = json.dumps({"event": node_name, "state": node_update})
      yield f"data: {payload}\n\n"
  ```

**Issue 4: LangSmith 403 noise polluting logs**
- **Where:** Any module that imports LangChain components.
- **Cause:** LangChain automatically enables LangSmith tracing at import time if `LANGCHAIN_TRACING_V2` is not explicitly set to `"false"` *before* the first LangChain import executes. Setting it in `.env` is too late; the env file is loaded after Python begins importing.
- **Fix:** Added `os.environ["LANGCHAIN_TRACING_V2"] = "false"` as the first statement in `backend/main.py`, before any LangChain import. This suppresses the 403 errors without requiring a LangSmith account.

---

## 🏗 Phase 4 — Real Integrations ✅

**Goal:** Replace every mock tool with a live implementation wired to real external services. The agent pipeline now runs end-to-end against a real Gmail inbox, a real Supabase database, HubSpot CRM, and a Slack workspace — with no mocks in the critical path.

### What was built

- **Real Gmail Email Tool** (`backend/tools/email_tools.py`) — Replaces `mock_email_tools.py`. Implements `fetch_invoices_from_email()` against the live Gmail API:
  - **OAuth2 credentials** loaded from `token.json` via `google.oauth2.credentials.Credentials.from_authorized_user_file()`. If the access token is expired, `google-auth` refreshes it automatically using the embedded refresh token — no manual re-authentication needed on every run.
  - **Thread pool execution** — All `googleapiclient` calls are synchronous (blocking I/O). Every Gmail API call is wrapped in `asyncio.get_running_loop().run_in_executor(None, ...)` so the event loop is never blocked.
  - **Gemini structured extraction** — Raw email body text is sent to `gemini-2.5-flash` via `ChatGoogleGenerativeAI.with_structured_output(InvoiceExtraction)`. The `InvoiceExtraction` Pydantic model has all fields typed as `Optional` — so emails where Gemini can’t extract a field return `None` for that field rather than raising a validation error. Temperature is set to `0` for deterministic extraction.
  - **`InvoiceExtraction` Pydantic model** — Defines the structured output schema: `invoice_id`, `vendor_name`, `customer_name`, `customer_id`, `line_items` (list of `LineItem`), `total_amount`, `invoice_date`, `due_date`, `currency`, `status`. All optional to tolerate partial parses.
  - **Per-email error isolation** — Each email is processed inside its own `try/except`. A single bad email (empty body, Gemini quota error, network blip) is logged and skipped — it never aborts the rest of the batch.
  - **Gmail label deduplication** — After a successful extraction the message is tagged with the `invoice-processed` label via `messages().modify()`. The search query is automatically amended with `-label:invoice-processed` so Gmail itself filters already-processed messages at the API level. No already-labelled message is ever fetched, decoded, or sent to Gemini again. The label is only applied on success — a failed extraction leaves the message unlabelled so it is retried on the next run.
  - **`_ensure_label_exists()`** — Called once per run. Lists all Gmail labels and creates `invoice-processed` if absent, returning the stable `label_id` for use in all `modify()` calls without an extra round-trip per message.
  - **`GMAIL_SCOPES`** upgraded from `gmail.readonly` to `gmail.modify` to permit label writes.

- **Real HubSpot CRM Tool** (`backend/tools/api_tools.py`) — Replaces `mock_api_tools.py`. Implements `push_invoice_to_crm()` using HubSpot's Deals API via `httpx.AsyncClient`:
  - **Search-then-Create idempotency** — Before creating a deal, the tool searches HubSpot for an existing deal with the same `dealname` (`"{invoice_id} — {vendor_name}"`). If found, it returns the existing Deal ID with no write. This makes the function safe to call multiple times on the same invoice (e.g. during LangGraph retries) without creating duplicate deals.
  - **Deal field mapping** — `dealname`, `amount`, `dealstage` (`appointmentscheduled`), `pipeline` (`default`), `closedate` (from `due_date`, only if present).

- **Real Database Tool** (`backend/tools/database_tools.py`) — Replaces `mock_database_tools.py`. Writes validated invoices to a dedicated Supabase business records table using the shared `psycopg3` pool.

- **Real Slack Notification Tool** (`backend/tools/notification_tools.py`) — Replaces `mock_notification_tools.py`. Posts workflow summaries to Slack via `slack_sdk.AsyncWebClient`, returning the message `ts` as a stable message ID for traceability.

- **All agent imports updated** — `extraction_agent.py`, `integration_agent.py`, and `notification_agent.py` now import from the real tool modules. `workflow_id` is injected into the invoice dict before the concurrent DB/CRM calls so `database_tools.py` can namespace rows correctly.

- **Developer reset utility** (`backend/tools/reset_invoice_labels.py`) — One-shot script to remove the `invoice-processed` label from all matching emails, resetting the inbox for a fresh extraction run. Used during development and testing to replay the pipeline without modifying Gmail manually.
  ```
  python -m backend.tools.reset_invoice_labels
  ```

- **`settings.py` extended** — Added `gmail_processed_label_name` (default: `invoice-processed`) so the label name is configurable per environment without a code change.

### 🐛 Issues Encountered & Resolutions

**Issue 1: `total_amount = None` causes `TypeError` in Transform Agent arithmetic**
- **Where:** `backend/agents/transform_agent.py`, `_validate_invoice()` Check 2.
- **Cause:** The mock tool omitted the `total_amount` key entirely for INV-2024-003, so `"total_amount" in invoice` returned `False` and the arithmetic was skipped. Real Gemini output uses `model_dump(exclude_none=False)`, which always serialises every field — so `total_amount` is present in the dict but set to `None` when the field couldn’t be extracted. The guard `"total_amount" in invoice` returned `True`, then `abs(None - 3.40)` raised `TypeError: unsupported operand type(s) for -: 'NoneType' and 'float'`.
- **Fix:** Unified Check 1 and Check 2 to treat `None` values as missing:
  - Check 1 changed from `_REQUIRED_FIELDS - invoice.keys()` to `{f for f in _REQUIRED_FIELDS if invoice.get(f) is None}` — catches both absent keys and `None` values.
  - Check 2 changed from `if "total_amount" in invoice` to `declared_total = invoice.get("total_amount"); if declared_total is not None` — arithmetic is never attempted on `None`.

**Issue 2: HubSpot 400 Bad Request — `currency` is not a valid deal property**
- **Where:** `backend/tools/api_tools.py`, `_create_deal()`, line sending `"currency"` in the properties payload.
- **Cause:** `currency` was included in the HubSpot deal create payload. The HubSpot Deals API does not accept `currency` as a per-deal property on the create endpoint — it is a portal-level setting. HubSpot returns `400 Bad Request` when unknown or unsupported properties are included.
- **Fix:** Removed `"currency"` from the `properties` dict in `_create_deal()`. Added an inline comment explaining the omission so it is not accidentally re-added.

**Issue 3: `NotNullViolation` on `crm_record_id` — schema mismatch with partial-success design**
- **Where:** `backend/agents/integration_agent.py`, `_insert_integration_result()` — triggered after the HubSpot 400.
- **Cause:** The `integration_agent` is explicitly designed for partial success: if one of the two concurrent tool calls (DB write, CRM push) fails, `asyncio.gather(return_exceptions=True)` still returns the successful result and sets the failed one to `None`. The row is then written with `NULL` for the failed column. However, the actual Supabase table was created with `crm_record_id TEXT NOT NULL` and `db_confirmation_id TEXT NOT NULL`, contradicting the nullable intent documented in the agent's docstring.
- **Fix:** Migration applied in the Supabase SQL Editor:
  ```sql
  ALTER TABLE integration_results
      ALTER COLUMN crm_record_id      DROP NOT NULL,
      ALTER COLUMN db_confirmation_id DROP NOT NULL;
  ```

**Issue 4: Gmail label deduplication causes empty extraction on re-runs**
- **Where:** `fetch_invoices_from_email()` search query, after the first successful run.
- **Cause:** The search query `subject:Invoice -label:invoice-processed` correctly excludes already-processed emails. After the first successful run all 3 test emails were labelled, so subsequent runs returned 0 messages. The pipeline completed with no data.
- **Fix:** Created `backend/tools/reset_invoice_labels.py` — a developer utility that searches for emails carrying the `invoice-processed` label and removes it via `messages().modify()`, making them visible to the extraction query again. Intended for development and test resets only.

**Issue 5: OAuth scope upgrade requires `token.json` re-authentication**
- **Where:** `backend/tools/email_tools.py` `GMAIL_SCOPES`, `backend/verify_connections.py`.
- **Cause:** The original scope `gmail.readonly` does not permit `messages().modify()`, which is required to apply the processed label. Upgrading `GMAIL_SCOPES` to `gmail.modify` invalidates the existing `token.json` because the stored scopes no longer match.
- **Fix:** Deleted the existing `token.json` and re-ran the OAuth flow via `python -m backend.verify_connections`. Google’s browser consent screen prompted for the new `modify` scope, producing a fresh `token.json` with the correct scope grant.

**Issue 6: Gemini free-tier quota exhausted — `RESOURCE_EXHAUSTED 429`**
- **Where:** `email_tools.py` Gemini extraction call, during the first smoke test run with real emails.
- **Cause:** The free-tier daily quota for `gemini-2.0-flash` was already exhausted. The error is per-model-per-day on the free tier.
- **Fix:** Upgraded the model in `email_tools.py` from `gemini-2.0-flash` to `gemini-2.5-flash`, which had available quota. Also noted for production: use a paid tier or implement exponential backoff with per-model fallback.

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
│   │   └── evaluator_agent.py    # Scores workflow output quality (Phase 6)
│   ├── config/
│   │   └── settings.py           # Pydantic BaseSettings — all env config
│   ├── db/                       # Database layer (Phase 3)
│   │   └── pool.py               # Shared psycopg3 AsyncConnectionPool lifecycle
│   ├── models/
│   │   ├── workflow.py           # WorkflowRequest, Task, TaskStatus, AgentType
│   │   └── execution.py          # ExecutionLog, ExecutionError, WorkflowResult
│   ├── orchestration/
│   │   ├── state_manager.py      # WorkflowState TypedDict + get_initial_state()
│   │   ├── error_handler.py      # ErrorClassifier, RetryConfig, execute_with_retry()
│   │   └── langgraph_workflow.py # LangGraph StateGraph + get_runnable_workflow()
│   ├── tools/                    # Tool implementations for agent use
│   │   ├── email_tools.py        # Real Gmail OAuth2 extraction (Phase 4)
│   │   ├── database_tools.py     # Real Supabase DB writes (Phase 4)
│   │   ├── api_tools.py          # Real HubSpot CRM push (Phase 4)
│   │   ├── notification_tools.py # Real Slack notifications (Phase 4)
│   │   └── reset_invoice_labels.py # Dev utility — reset Gmail processed labels
|   |
│   ├── main.py                   # FastAPI app — lifespan · SSE stream · status endpoint
│   └── verify_connections.py     # Diagnostic script for all external services
├── tests/                        # Test suite (Phase 4+)
├── run.py                        # Windows-safe uvicorn entry point (Phase 3)
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

5. Start the API server:
   ```bash
   python run.py           # development (Windows-safe, uses SelectorEventLoop)
   python run.py --reload  # with hot-reload
   ```

---

## 🔑 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | Google Gemini API key for LLM calls |
| `SUPABASE_DATABASE_URL` | ✅ | Async PostgreSQL connection string (`postgresql://...`) |
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash Redis REST endpoint URL |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash Redis REST token |
| `LANGFUSE_PUBLIC_KEY` | ✅ | Langfuse public key for tracing |
| `LANGFUSE_SECRET_KEY` | ✅ | Langfuse secret key for tracing |
| `LANGFUSE_HOST` | ❌ | Langfuse instance URL (defaults to `https://cloud.langfuse.com`) |
| `HUBSPOT_ACCESS_TOKEN` | ✅ | HubSpot private app access token for CRM deal writes |
| `GMAIL_CREDENTIALS_PATH` | ❌ | Path to Google OAuth2 client secrets JSON (default: `credentials.json`) |
| `GMAIL_TOKEN_PATH` | ❌ | Path to the stored Gmail OAuth2 token JSON (default: `token.json`) |
| `GMAIL_SEARCH_QUERY` | ❌ | Gmail search query for invoice emails (default: `subject:Invoice`) |
| `GMAIL_PROCESSED_LABEL_NAME` | ❌ | Gmail label applied after extraction to prevent re-processing (default: `invoice-processed`) |
| `SLACK_BOT_TOKEN` | ✅ | Slack bot token (`xoxb-...`) for posting workflow summaries |
| `SLACK_CHANNEL_ID` | ✅ | Slack channel ID to post notifications into |
| `ENVIRONMENT` | ❌ | App environment — `development`, `staging`, `production` (defaults to `development`) |

---

## 📄 License

[Add license information here]

---

*Follow along on LinkedIn as each phase ships — real engineering decisions, not just demos.*
