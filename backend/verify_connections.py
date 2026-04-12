import asyncio
import logging
import sys

from langchain_google_genai import ChatGoogleGenerativeAI
from langfuse import Langfuse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from upstash_redis.asyncio import Redis

import os
os.environ["LANGCHAIN_TRACING_V2"] = "false"

# Ensure this script is run from the project root: `python -m backend.verify_connections`
from backend.config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


async def verify_postgres():
    """Verify Supabase PostgreSQL connection by running a simple query."""
    logging.info("Checking Supabase PostgreSQL connection...")
    try:
        engine = create_async_engine(settings.supabase_database_url)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            if result.scalar() == 1:
                logging.info("✔ Supabase PostgreSQL connected successfully.")
            else:
                raise Exception("Connected but received unexpected query result.")
        await engine.dispose()
        return True
    except Exception as e:
        logging.error(f"❌ Postgres Error: {e}")
        return False


async def verify_redis():
    """Verify Upstash Redis connection by pinging the server."""
    logging.info("Checking Upstash Redis connection...")
    try:
        redis = Redis(
            url=settings.upstash_redis_rest_url,
            token=settings.upstash_redis_rest_token
        )
        await redis.set("ping", "pong")
        val = await redis.get("ping")
        if val == "pong":
            logging.info("✔ Upstash Redis connected successfully.")
        else:
            raise Exception("Failed to retrieve test value from Upstash.")
        await redis.delete("ping")
        return True
    except Exception as e:
        logging.error(f"❌ Upstash Redis Error: {e}")
        return False


def verify_langfuse():
    """Verify Langfuse API credentials using auth_check."""
    logging.info("Checking Langfuse credentials...")
    try:
        langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host
        )
        if langfuse.auth_check():
            logging.info("✔ Langfuse authenticated successfully.")
            return True
        else:
            raise Exception("Langfuse auth_check failed.")
    except Exception as e:
        logging.error(f"❌ Langfuse Error: {e}")
        return False


def verify_gemini():
    """Verify Google Gemini LLM API by invoking a test completion."""
    logging.info("Checking Google Gemini LLM API...")
    try:
        llm = ChatGoogleGenerativeAI(
            model="gemma-3-27b-it",
            google_api_key=settings.gemini_api_key
        )
        response = llm.invoke("Respond with exactly one word: 'ok'")
        if response and response.content:
            logging.info("✔ Google Gemini connection successful.")
            return True
        else:
            raise Exception("Empty response from Gemini API.")
    except Exception as e:
        logging.error(f"❌ Gemini API Error: {e}")
        return False


async def main():
    logging.info("Starting diagnostic check for external services...\n")

    # Run strictly async services
    pg_ok = await verify_postgres()
    rd_ok = await verify_redis()

    # Run blocking sync checking services
    lf_ok = verify_langfuse()
    gm_ok = verify_gemini()

    print("\n--- Diagnostic Results ---")
    if all([pg_ok, rd_ok, lf_ok, gm_ok]):
        logging.info("🎉 All external services are correctly configured and reachable.")
        sys.exit(0)
    else:
        logging.error("⚠️ One or more service checks failed. Please check your .env configurations.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
