"""
backend.tools.email_tools

Real Gmail implementation of the invoice email extraction tool.

Replaces backend.tools.mock_email_tools.  The function signature and return
schema are identical, so the Extraction Agent requires no import-path changes
beyond swapping the module name.

Design
------
Gmail API (synchronous)
    ``googleapiclient`` is a blocking HTTP library.  All Gmail API calls are
    wrapped in ``asyncio.get_running_loop().run_in_executor(None, ...)`` so
    they run in the default ``ThreadPoolExecutor`` and never block the event
    loop.

Credentials
    OAuth 2.0 credentials are loaded from ``settings.gmail_token_path``
    (``token.json``), which stores the access token *and* refresh token
    produced by the one-time ``InstalledAppFlow``.  If the access token is
    expired, ``google-auth`` refreshes it automatically using the client
    secrets embedded in the token file together with
    ``settings.gmail_credentials_path`` (``credentials.json``).

Gemini extraction
    Raw email body text is forwarded to ``gemini-2.0-flash`` via
    ``ChatGoogleGenerativeAI`` with ``with_structured_output(InvoiceExtraction)``.
    All invoice fields are ``Optional`` so that partially parseable emails
    (e.g. a missing total) are still returned rather than raising a
    validation error.

Per-email isolation
    Each email is extracted independently inside a try/except block.  A
    single unparseable email is logged and skipped; it does not abort the
    rest of the batch.

Deduplication via Gmail labels
    After a successful extraction the message is tagged with the label
    defined in ``settings.gmail_processed_label_name`` (default:
    ``invoice-processed``) via the ``messages().modify()`` API.  The search
    query is automatically amended with ``-label:<name>`` so that already-
    labelled messages are excluded from future runs at the Gmail API level —
    no messages are fetched, decoded, or sent to Gemini a second time.

    ``_ensure_label_exists`` is called once per ``fetch_invoices_from_email``
    invocation.  It lists all user labels and creates the label if absent,
    returning the stable ``label_id`` used by ``messages().modify()``.

    The label is applied *only* on success.  If Gemini extraction raises an
    exception the label is not applied, so the email will be re-tried on the
    next workflow run.

GMAIL_SCOPES
    ``https://www.googleapis.com/auth/gmail.modify`` is the minimum scope
    required.  Read-only access is sufficient because we only fetch message
    bodies; we never send, label, or delete messages.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from backend.config.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# gmail.modify is required to apply the processed label after extraction.
# gmail.readonly is insufficient — it does not allow messages().modify().
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Maximum number of matching emails to fetch per run.  Guards against
# runaway API quota consumption if the search query is too broad.
MAX_RESULTS = 20


# ---------------------------------------------------------------------------
# Pydantic model for structured Gemini output
# ---------------------------------------------------------------------------


class LineItem(BaseModel):
    """A single line item on an invoice."""

    description: str = Field(description="Description of the product or service")
    quantity: int = Field(description="Number of units")
    unit_price: float = Field(description="Price per unit")


class InvoiceExtraction(BaseModel):
    """
    Structured invoice fields extracted from raw email body text by Gemini.

    All fields are Optional so that a partial parse (e.g. a missing
    total_amount like the INV-2024-003 test case) is returned as-is rather
    than raising a Pydantic validation error.  The Transform Agent is
    responsible for detecting and flagging missing required fields.
    """

    invoice_id: Optional[str] = Field(
        default=None,
        description="Unique invoice reference number (e.g. INV-2024-001)",
    )
    vendor_name: Optional[str] = Field(
        default=None,
        description="Name of the company issuing the invoice",
    )
    customer_name: Optional[str] = Field(
        default=None,
        description="Name of the company being billed",
    )
    customer_id: Optional[str] = Field(
        default=None,
        description="Internal customer identifier (e.g. CUST-10042)",
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="Itemised list of products or services on the invoice",
    )
    total_amount: Optional[float] = Field(
        default=None,
        description="Declared invoice total (may be absent if the email was unreadable)",
    )
    invoice_date: Optional[str] = Field(
        default=None,
        description="Invoice issue date in ISO 8601 format (YYYY-MM-DD)",
    )
    due_date: Optional[str] = Field(
        default=None,
        description="Invoice payment due date in ISO 8601 format (YYYY-MM-DD)",
    )
    currency: Optional[str] = Field(
        default=None,
        description="ISO 4217 currency code (e.g. USD, EUR, GBP)",
    )
    status: Optional[str] = Field(
        default=None,
        description="Invoice payment status: one of pending | paid | unpaid",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_credentials() -> Credentials:
    """
    Load and refresh Gmail OAuth2 credentials from disk.

    Reads ``settings.gmail_token_path`` (``token.json``) which contains the
    access + refresh tokens produced by the one-time OAuth flow.  If the
    access token has expired, ``google-auth`` uses the refresh token and the
    OAuth client secrets in ``settings.gmail_credentials_path``
    (``credentials.json``) to obtain a new access token automatically.

    Returns:
        A valid, possibly freshly refreshed ``google.oauth2.credentials.Credentials``
        instance ready for use with ``googleapiclient.discovery.build``.

    Raises:
        FileNotFoundError: If ``token.json`` does not exist (OAuth flow has
            never been run for this environment).
        google.auth.exceptions.RefreshError: If the refresh token is revoked
            or the OAuth client has been deleted in Google Cloud Console.
    """
    creds = Credentials.from_authorized_user_file(
        settings.gmail_token_path,
        scopes=GMAIL_SCOPES,
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            logger.info("email_tools: access token expired — refreshing via refresh token")
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail credentials are invalid and cannot be refreshed.  "
                "Re-run the OAuth flow to generate a fresh token.json."
            )

    return creds


def _fetch_message_ids(service, query: str, max_results: int) -> list[str]:
    """
    List Gmail message IDs matching *query*.

    Args:
        service:    An authenticated Gmail API service resource.
        query:      Gmail search query (e.g. ``"subject:Invoice"``).
        max_results: Maximum number of message IDs to return.

    Returns:
        List of message ID strings (may be empty if no messages match).
    """
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    messages = result.get("messages", [])
    return [m["id"] for m in messages]


def _fetch_message_body(service, message_id: str) -> str:
    """
    Fetch and decode the plain-text body of a single Gmail message.

    Prefers the ``text/plain`` MIME part; falls back to ``text/html`` if no
    plain-text part is found; falls back to the top-level ``body.data`` for
    simple (non-multipart) messages.

    Args:
        service:    An authenticated Gmail API service resource.
        message_id: The Gmail message ID to fetch.

    Returns:
        The decoded message body as a UTF-8 string.  Returns an empty string
        if no body data could be extracted.
    """
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )

    payload = msg.get("payload", {})
    parts = payload.get("parts", [])

    def _decode(data: str) -> str:
        """URL-safe base64 → UTF-8 string."""
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    # Multipart message: walk parts for text/plain first, then text/html
    for mime_type in ("text/plain", "text/html"):
        for part in parts:
            if part.get("mimeType") == mime_type:
                body_data = part.get("body", {}).get("data", "")
                if body_data:
                    return _decode(body_data)

    # Simple (non-multipart) message
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return _decode(body_data)

    return ""


def _ensure_label_exists(service, label_name: str) -> str:
    """
    Return the Gmail label ID for *label_name*, creating the label if absent.

    Called once per ``fetch_invoices_from_email`` invocation so the label ID
    is resolved before processing individual messages.  Creating the label
    is idempotent — if two concurrent runs call this simultaneously, the
    second call will find the label already present and return its ID.

    Args:
        service:    An authenticated Gmail API service resource.
        label_name: Human-readable label name (e.g. ``"invoice-processed"``),
                    taken from ``settings.gmail_processed_label_name``.

    Returns:
        The stable Gmail label ID string (e.g. ``"Label_1234567890"``).
    """
    labels_response = service.users().labels().list(userId="me").execute()
    for label in labels_response.get("labels", []):
        if label["name"] == label_name:
            logger.debug("email_tools: found existing label '%s' id=%s", label_name, label["id"])
            return label["id"]

    # Label does not exist — create it
    new_label = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    logger.info("email_tools: created Gmail label '%s' id=%s", label_name, new_label["id"])
    return new_label["id"]


def _mark_message_processed(service, message_id: str, label_id: str) -> None:
    """
    Apply the processed label to a Gmail message.

    Uses ``messages().modify()`` to add the label without touching any other
    labels or moving the message.  This is the only write operation performed
    against the Gmail API; all other calls are read-only.

    Args:
        service:    An authenticated Gmail API service resource.
        message_id: The Gmail message ID to label.
        label_id:   The Gmail label ID returned by ``_ensure_label_exists``.
    """
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id], "removeLabelIds": []},
    ).execute()
    logger.debug("email_tools: applied label_id=%s to message_id=%s", label_id, message_id)


def _build_gmail_service():
    """
    Build an authenticated Gmail API service resource (synchronous).

    Loads credentials, refreshes if necessary, and returns a
    ``googleapiclient`` resource ready for API calls.

    Returns:
        An authenticated ``googleapiclient.discovery.Resource`` for Gmail v1.
    """
    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------


async def fetch_invoices_from_email() -> list[dict]:
    """
    Fetch invoice emails from Gmail and extract structured invoice data using Gemini.

    Workflow
    --------
    1. Load OAuth2 credentials from disk (refresh if expired).
    2. Build the Gmail API service in a thread pool executor (blocking I/O).
    3. Search for emails matching ``settings.gmail_search_query``.
    4. For each matching email, fetch the message body in the thread pool.
    5. Pass the raw body text to ``gemini-2.0-flash`` with structured output
       (``InvoiceExtraction`` Pydantic model) for field extraction.
    6. Convert the Pydantic model to a plain dict and collect results.

    Returns:
        List of invoice dicts.  Each dict contains some or all of:
            invoice_id    : str   — unique invoice reference number
            vendor_name   : str   — company issuing the invoice
            customer_name : str   — company being billed
            customer_id   : str   — internal customer identifier
            line_items    : list  — itemised charges (description, quantity, unit_price)
            total_amount  : float — declared total (may be None if extraction failed)
            invoice_date  : str   — ISO 8601 date (YYYY-MM-DD)
            due_date      : str   — ISO 8601 date (YYYY-MM-DD)
            currency      : str   — ISO 4217 currency code
            status        : str   — one of: pending | paid | unpaid

    Raises:
        FileNotFoundError: If ``token.json`` does not exist.
        google.auth.exceptions.RefreshError: If the refresh token is revoked.
        googleapiclient.errors.HttpError: On Gmail API errors (e.g. quota exceeded).
    """
    loop = asyncio.get_running_loop()

    # ── Step 1 & 2: Build authenticated Gmail service in thread pool ──────────
    logger.info("email_tools: building Gmail API service")
    service = await loop.run_in_executor(None, _build_gmail_service)

    # ── Step 2b: Resolve (or create) the processed label once ────────────────
    # Done before the search so the label ID is available for every message
    # in the batch without an extra round-trip per message.
    label_id: str = await loop.run_in_executor(
        None,
        _ensure_label_exists,
        service,
        settings.gmail_processed_label_name,
    )

    # ── Step 3: Fetch matching message IDs ───────────────────────────────────
    # Append "-label:<name>" so Gmail itself filters out already-processed
    # messages. This avoids fetching, decoding, or sending them to Gemini.
    dedup_query = f"{settings.gmail_search_query} -label:{settings.gmail_processed_label_name}"
    logger.info(
        "email_tools: searching Gmail with query=%r (max_results=%d)",
        dedup_query,
        MAX_RESULTS,
    )
    message_ids: list[str] = await loop.run_in_executor(
        None,
        _fetch_message_ids,
        service,
        dedup_query,
        MAX_RESULTS,
    )

    if not message_ids:
        logger.info("email_tools: no unprocessed emails found — returning empty list")
        return []

    logger.info("email_tools: found %d matching email(s)", len(message_ids))

    # ── Step 4 & 5: Build Gemini LLM with structured output ──────────────────
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        google_api_key=settings.gemini_api_key,
        temperature=0,  # Deterministic extraction — no creativity needed
    )
    structured_llm = llm.with_structured_output(InvoiceExtraction)

    extraction_prompt = (
        "You are an invoice data extraction assistant.\n"
        "Extract all invoice fields from the email body text below.\n"
        "Return only what is explicitly stated in the email.\n"
        "If a field is not present in the email, leave it as null.\n"
        "Dates must be in ISO 8601 format (YYYY-MM-DD).\n"
        "Status should be one of: pending, paid, unpaid.\n\n"
        "Email body:\n\n{body}"
    )

    # ── Step 5 & 6: Fetch bodies, extract, and collect results ───────────────
    invoices: list[dict] = []

    for msg_id in message_ids:
        try:
            # Fetch body in thread pool (blocking I/O)
            body: str = await loop.run_in_executor(
                None,
                _fetch_message_body,
                service,
                msg_id,
            )

            if not body.strip():
                logger.warning(
                    "email_tools: message_id=%s has an empty body — skipping",
                    msg_id,
                )
                continue

            logger.info(
                "email_tools: extracting invoice fields from message_id=%s via Gemini",
                msg_id,
            )

            # Gemini structured extraction (async — no executor needed)
            prompt = extraction_prompt.format(body=body)
            extraction: InvoiceExtraction = await structured_llm.ainvoke(prompt)

            # Convert Pydantic model → plain dict for downstream agents.
            # line_items are also converted from LineItem models to plain dicts.
            invoice_dict = extraction.model_dump(exclude_none=False)
            invoice_dict["line_items"] = [
                item.model_dump() for item in extraction.line_items
            ]

            invoices.append(invoice_dict)
            logger.info(
                "email_tools: extracted invoice_id=%r from message_id=%s",
                extraction.invoice_id,
                msg_id,
            )

            # ── Apply processed label ─────────────────────────────────────────
            # Label is applied AFTER a successful extraction only.
            # A failed extraction (caught below) leaves the message unlabelled
            # so it will be retried on the next workflow run.
            await loop.run_in_executor(
                None,
                _mark_message_processed,
                service,
                msg_id,
                label_id,
            )

        except Exception as exc:  # noqa: BLE001
            # Isolate per-email failures — log and continue with the rest
            logger.error(
                "email_tools: failed to extract invoice from message_id=%s — %s: %s",
                msg_id,
                type(exc).__name__,
                exc,
            )
            continue

    logger.info(
        "email_tools: extraction complete — %d invoice(s) returned from %d email(s)",
        len(invoices),
        len(message_ids),
    )
    return invoices
