"""
backend/tools/reset_invoice_labels.py

Developer utility — removes the "invoice-processed" Gmail label from all
matching emails so they will be picked up again on the next workflow run.

Use this when:
  - Re-running the smoke test after a previous successful extraction.
  - Resetting the inbox after a failed or partial run.
  - Testing the extraction pipeline from scratch.

Run from the project root:
    python -m backend.tools.reset_invoice_labels

The script uses the same credentials, token, scopes, and search query as
email_tools.py, so no extra configuration is needed.
"""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("reset_invoice_labels")

from backend.config.settings import settings
from backend.tools.email_tools import (
    GMAIL_SCOPES,
    _build_gmail_service,
    _fetch_message_ids,
)


def _get_label_id(service, label_name: str) -> str | None:
    """Return the Gmail label ID for label_name, or None if it doesn't exist."""
    resp = service.users().labels().list(userId="me").execute()
    for label in resp.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    return None


def reset_labels() -> None:
    label_name = settings.gmail_processed_label_name

    logger.info("Building Gmail service …")
    service = _build_gmail_service()

    # ── Find the label ────────────────────────────────────────────────────────
    label_id = _get_label_id(service, label_name)
    if label_id is None:
        logger.info("Label '%s' does not exist in Gmail — nothing to reset.", label_name)
        return

    logger.info("Found label '%s'  id=%s", label_name, label_id)

    # ── Find emails that currently carry the label ────────────────────────────
    # Search specifically FOR the label (no -label: exclusion here).
    query = f"{settings.gmail_search_query} label:{label_name}"
    logger.info("Searching with query: %r", query)

    message_ids = _fetch_message_ids(service, query, max_results=100)

    if not message_ids:
        logger.info("No labelled emails found — inbox is already clean.")
        return

    logger.info("Found %d labelled email(s) — removing label …", len(message_ids))

    # ── Remove label from each message ───────────────────────────────────────
    removed = 0
    for msg_id in message_ids:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [], "removeLabelIds": [label_id]},
        ).execute()
        logger.info("  ✔ Removed label from message_id=%s", msg_id)
        removed += 1

    logger.info("Done — label removed from %d email(s).", removed)
    logger.info("These emails will be picked up on the next workflow run.")


if __name__ == "__main__":
    reset_labels()
