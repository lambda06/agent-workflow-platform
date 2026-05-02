"""
run.py — Windows-safe uvicorn entry point.

Why this file exists:
    psycopg3 requires SelectorEventLoop on Windows. On uvicorn 0.44+, the loop
    is created via an explicit loop_factory, which BYPASSES asyncio's event loop
    policy entirely. The default factory on Windows is:

        asyncio_loop_factory() -> returns asyncio.ProactorEventLoop

    This is hardcoded in uvicorn/loops/asyncio.py. No amount of
    set_event_loop_policy() calls can override it because uvicorn never calls
    asyncio.new_event_loop() -- it calls the factory directly.

    uvicorn.run() does NOT expose loop_factory as a parameter. So we drop one
    level down and call uvicorn._compat.asyncio_run(server.serve(), ...) directly
    -- the same internal call server.run() makes -- but pass SelectorEventLoop
    as the factory instead of uvicorn's hardcoded Windows default.

Usage:
    python run.py           # development
    python run.py --reload  # hot reload
"""

import asyncio
import argparse
import sys

import uvicorn
from uvicorn._compat import asyncio_run


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reload", action="store_true", default=False)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    config = uvicorn.Config(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # On Windows, inject SelectorEventLoop so psycopg3's async pool works.
    # On Linux/macOS, pass None so uvicorn uses its default (also SelectorEventLoop).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio_run(server.serve(), loop_factory=loop_factory)
