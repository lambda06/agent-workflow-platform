"""
backend.db.pool

Centralised psycopg3 connection pool for all Supabase PostgreSQL interactions.

Why a single shared pool?
    The original design gave each agent its own module-level asyncpg pool. This
    caused two concrete problems found during smoke testing:

    1. Driver conflict: asyncpg and LangGraph's AsyncPostgresSaver both connect to
       the same Supabase instance using *different* PostgreSQL wire-protocol drivers
       (asyncpg vs psycopg3). Running them simultaneously caused SSL negotiation
       conflicts that broke the smoke test.

    2. Connection exhaustion: 4 agents × min_size=2 = 8 idle connections before any
       request arrives, plus the AsyncPostgresSaver's pool on top. Supabase free and
       pro plans cap connections at 60–200; leaking 8+ per process restart adds up fast.

    A single psycopg3 AsyncConnectionPool eliminates both problems:
        - One driver (psycopg3) is the same driver that AsyncPostgresSaver uses
          internally — no conflicts.
        - One pool with a single tunable max_size covers all agents and the
          LangGraph checkpointer.

Driver choice — psycopg3 (psycopg[binary]):
    LangGraph's AsyncPostgresSaver.from_pool() accepts a psycopg3
    AsyncConnectionPool. Aligning agent code to the same driver means:
        - The checkpointer can share this pool directly (pass pool to
          AsyncPostgresSaver(pool=...)).
        - One less dependency (asyncpg removed from requirements.txt).
        - psycopg3 is the actively maintained successor to psycopg2; asyncpg is
          performant but lower-level and lacks ORM/checkpointer ecosystem support.

Pool lifecycle:
    init_pool()  — called once from the FastAPI lifespan hook on startup.
    close_pool() — called once from the FastAPI lifespan hook on shutdown.
    get_pool()   — called by agents and the LangGraph workflow to borrow connections.

    Agents must NOT call init_pool() themselves. If get_pool() is called before
    init_pool(), it raises RuntimeError immediately — this surfaces wiring bugs
    early rather than producing confusing NoneType errors mid-request.

Supabase SSL:
    Supabase requires TLS. The connection string from settings.supabase_database_url
    already contains ?sslmode=require (the postgresql:// scheme accepted by psycopg3
    unlike asyncpg). No stripping or separate ssl= kwarg is needed.

Pool sizing (env-var tunable):
    POOL_MIN_SIZE  — minimum number of idle connections to keep open (default: 2).
    POOL_MAX_SIZE  — maximum connections the pool will open simultaneously (default: 10).
    Tune these when adding horizontal scaling or if Supabase connection limits are hit.
"""

import logging
import os

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from backend.config.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level pool handle — populated by init_pool(), never touched directly
# ---------------------------------------------------------------------------

_pool: AsyncConnectionPool | None = None


# ---------------------------------------------------------------------------
# Lifecycle — call from FastAPI lifespan
# ---------------------------------------------------------------------------

async def init_pool() -> None:
    """
    Open the shared psycopg3 connection pool.

    Must be called exactly once, before any agent or LangGraph node runs.
    The recommended place is the FastAPI lifespan startup hook in main.py.

    Pool configuration is read from environment variables with sensible defaults:
        POOL_MIN_SIZE (int, default 2)  — minimum idle connections.
        POOL_MAX_SIZE (int, default 10) — maximum open connections.

    Raises:
        psycopg_pool.PoolTimeout: If the initial connection cannot be established
            within the default timeout (30 s). Check SUPABASE_DATABASE_URL and
            network connectivity.
    """
    global _pool

    min_size = int(os.getenv("POOL_MIN_SIZE", "2"))
    max_size = int(os.getenv("POOL_MAX_SIZE", "10"))

    logger.info(
        "db.pool: opening psycopg3 AsyncConnectionPool "
        "(min_size=%d, max_size=%d) …",
        min_size,
        max_size,
    )

    # open=False defers actual connections until the first acquire(), avoiding
    # blocking the event loop during module import / pool creation.
    _pool = AsyncConnectionPool(
        conninfo=settings.supabase_database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row},
    )

    # Explicitly open so connections are ready before the first request arrives.
    await _pool.open()
    logger.info("db.pool: connection pool ready.")


async def close_pool() -> None:
    """
    Gracefully close the shared connection pool on application shutdown.

    Waits for any in-flight queries to complete before closing, so no work
    is interrupted. Call this from the FastAPI lifespan shutdown hook.
    """
    global _pool
    if _pool is not None:
        logger.info("db.pool: closing connection pool …")
        await _pool.close()
        _pool = None
        logger.info("db.pool: connection pool closed.")


# ---------------------------------------------------------------------------
# Accessor — used by agents and the LangGraph workflow
# ---------------------------------------------------------------------------

def get_pool() -> AsyncConnectionPool:
    """
    Return the shared psycopg3 AsyncConnectionPool.

    Usage in agent nodes::

        from backend.db.pool import get_pool

        pool = get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT ...")
            row = await cur.fetchone()

    Returns:
        The initialised AsyncConnectionPool instance.

    Raises:
        RuntimeError: If called before init_pool() — indicates a wiring bug
            (pool not opened in the FastAPI lifespan hook).
    """
    if _pool is None:
        raise RuntimeError(
            "db.pool: get_pool() called before init_pool(). "
            "Ensure init_pool() is awaited in the FastAPI lifespan startup hook "
            "before any agent node runs."
        )
    return _pool
