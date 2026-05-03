import asyncio
import logging
import os
import sys

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from psycopg_pool import AsyncConnectionPool
from langchain_google_genai import ChatGoogleGenerativeAI
from langfuse import Langfuse
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from upstash_redis.asyncio import Redis

os.environ["LANGCHAIN_TRACING_V2"] = "false"

# Ensure this script is run from the project root: `python -m backend.verify_connections`
from backend.config.settings import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


async def verify_postgres():
    """Verify Supabase PostgreSQL connection using a one-shot psycopg3 pool."""
    logging.info("Checking Supabase PostgreSQL connection...")
    try:
        pool = AsyncConnectionPool(
            conninfo=settings.supabase_database_url,
            min_size=1,
            max_size=2,
            open=False,
        )
        await pool.open()
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1")
            row = await cur.fetchone()
            if row[0] != 1:
                raise Exception("Connected but received unexpected query result.")
        await pool.close()
        logging.info("✔ Supabase PostgreSQL connected successfully.")
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


# ---------------------------------------------------------------------------
# Gmail — Google API Python Client (same auth files the MCP server uses)
# ---------------------------------------------------------------------------
_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


async def verify_gmail():
    """Verify Gmail OAuth2 credentials and run a test search query."""
    logging.info("Checking Gmail connection...")
    try:
        creds = None
        token_path = settings.gmail_token_path
        creds_path = settings.gmail_credentials_path

        # Load cached token if it exists
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, _GMAIL_SCOPES)

        # Refresh or re-authorise if needed
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Non-interactive environments will raise here if credentials
                # are missing — that is the correct behaviour for a health check.
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, _GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            # Persist the refreshed token
            with open(token_path, "w") as f:
                f.write(creds.to_json())

        service = build("gmail", "v1", credentials=creds)
        result = (
            service.users()
            .messages()
            .list(userId="me", q=settings.gmail_search_query, maxResults=10)
            .execute()
        )
        count = result.get("resultSizeEstimate", 0)
        logging.info(
            f'✔ Gmail connected. Found {count} email(s) matching "{settings.gmail_search_query}".'
        )
        return True
    except Exception as e:
        logging.error(f"❌ Gmail Error: {e}")
        return False


# ---------------------------------------------------------------------------
# HubSpot — REST API via httpx
# ---------------------------------------------------------------------------


async def verify_hubspot():
    """Verify HubSpot API access by fetching a single contact record."""
    logging.info("Checking HubSpot connection...")
    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {"Authorization": f"Bearer {settings.hubspot_access_token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers, params={"limit": 1})
        if response.status_code == 200:
            logging.info("✔ HubSpot API — PASS (200 OK).")
            return True
        else:
            raise Exception(f"Unexpected status {response.status_code}: {response.text[:200]}")
    except Exception as e:
        logging.error(f"❌ HubSpot Error: {e}")
        return False


# ---------------------------------------------------------------------------
# Slack — slack_sdk AsyncWebClient
# ---------------------------------------------------------------------------


async def verify_slack():
    """Verify Slack bot token by calling auth.test."""
    logging.info("Checking Slack connection...")
    try:
        client = AsyncWebClient(token=settings.slack_bot_token)
        response = await client.auth_test()
        bot_name = response.get("bot_id") or response.get("user", "<unknown>")
        logging.info(f"✔ Slack authenticated. Bot name: {response.get('user', bot_name)}.")
        return True
    except SlackApiError as e:
        logging.error(f"❌ Slack API Error: {e.response['error']}")
        return False
    except Exception as e:
        logging.error(f"❌ Slack Error: {e}")
        return False


async def main():
    logging.info("Starting diagnostic check for external services...\n")

    # Run strictly async services
    pg_ok = await verify_postgres()
    rd_ok = await verify_redis()

    # Run blocking sync checking services
    lf_ok = verify_langfuse()
    gm_ok = verify_gemini()

    # New async integration checks
    gmail_ok = await verify_gmail()
    hs_ok = await verify_hubspot()
    sl_ok = await verify_slack()

    print("\n--- Diagnostic Results ---")
    if all([pg_ok, rd_ok, lf_ok, gm_ok, gmail_ok, hs_ok, sl_ok]):
        logging.info("🎉 All external services are correctly configured and reachable.")
        sys.exit(0)
    else:
        logging.error("⚠️ One or more service checks failed. Please check your .env configurations.")
        sys.exit(1)


if __name__ == "__main__":
    # On Windows, asyncio.run() defaults to ProactorEventLoop which psycopg3
    # cannot use. Force SelectorEventLoop before entering the event loop —
    # the same fix applied in run.py for uvicorn. No-op on Linux/macOS.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
