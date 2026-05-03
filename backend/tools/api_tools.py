"""
backend.tools.api_tools

Real HubSpot CRM implementation of the invoice-to-deal push tool.

HubSpot Deal field mappings
---------------------------
Invoice field       HubSpot deal property       Notes
-----------         --------------------        -----
invoice_id          (embedded in dealname)      "{invoice_id} — {vendor_name}"
vendor_name         (embedded in dealname)      See above
total_amount        amount                      Float; HubSpot stores as string
due_date            closedate                   ISO 8601 date string (YYYY-MM-DD)
currency            currency                    ISO 4217 code, e.g. "USD"
(fixed)             dealstage                   "appointmentscheduled" (pipeline default)
(fixed)             pipeline                    "default"

Idempotency — Search-then-Create
---------------------------------
HubSpot's Deals API has NO native uniqueness constraint on dealname or any
standard property.  Posting the same payload twice creates two separate deals
— there is no 409 response for duplicate deal names.

The correct approach (per HubSpot documentation) is Search-then-Create:
    1. Search for an existing deal whose ``dealname`` equals the target value.
    2. If found → return the existing deal ID (no write performed).
    3. If not found → create the deal and return the new ID.

This makes ``push_invoice_to_crm`` safe to call multiple times for the same
invoice (e.g. when LangGraph retries the integration_node after a partial
failure), without creating phantom duplicate deals in HubSpot.

API endpoints used
------------------
Search:  POST https://api.hubapi.com/crm/v3/objects/deals/search
Create:  POST https://api.hubapi.com/crm/v3/objects/deals
Auth:    Authorization: Bearer {settings.hubspot_access_token}
"""

import logging

import httpx

from backend.config.settings import settings

logger = logging.getLogger(__name__)

_HUBSPOT_DEALS_URL = "https://api.hubapi.com/crm/v3/objects/deals"
_HUBSPOT_SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/deals/search"
_DEFAULT_TIMEOUT = 15.0  # seconds


def _auth_headers() -> dict:
    """Return the Authorization header for all HubSpot requests."""
    return {
        "Authorization": f"Bearer {settings.hubspot_access_token}",
        "Content-Type": "application/json",
    }


def _build_deal_name(invoice: dict) -> str:
    """
    Construct the canonical deal name used as the idempotency key.

    Format: "{invoice_id} — {vendor_name}"
    The invoice_id prefix makes it unique per invoice; embedding vendor_name
    makes it human-readable in the HubSpot UI without needing custom properties.
    """
    invoice_id: str = invoice.get("invoice_id", "unknown")
    vendor_name: str = invoice.get("vendor_name", "Unknown Vendor")
    return f"{invoice_id} \u2014 {vendor_name}"


async def _search_existing_deal(client: httpx.AsyncClient, deal_name: str) -> str | None:
    """
    Search HubSpot for a deal with a matching dealname.

    Args:
        client:     Shared httpx.AsyncClient for the current tool call.
        deal_name:  The canonical deal name to search for (exact match).

    Returns:
        The HubSpot deal ID string if found, otherwise None.

    Raises:
        httpx.HTTPStatusError: On a non-2xx response from the search endpoint.
    """
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "dealname",
                        "operator": "EQ",
                        "value": deal_name,
                    }
                ]
            }
        ],
        "properties": ["dealname"],
        "limit": 1,
    }
    response = await client.post(
        _HUBSPOT_SEARCH_URL,
        headers=_auth_headers(),
        json=payload,
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    results = data.get("results", [])
    if results:
        deal_id: str = results[0]["id"]
        return deal_id
    return None


async def _create_deal(client: httpx.AsyncClient, invoice: dict, deal_name: str) -> str:
    """
    Create a new HubSpot Deal from the validated invoice data.

    Args:
        client:     Shared httpx.AsyncClient for the current tool call.
        invoice:    Validated invoice dict from the transform agent.
        deal_name:  Pre-built canonical deal name (avoids recomputing).

    Returns:
        The HubSpot-assigned deal ID string.

    Raises:
        httpx.HTTPStatusError: On a non-2xx response from the create endpoint.
    """
    properties: dict = {
        "dealname": deal_name,
        "amount":    str(invoice.get("total_amount", 0.0)),
        "dealstage": "appointmentscheduled",
        "pipeline":  "default",
        # NOTE: "currency" is intentionally omitted.
        # HubSpot Deals API does not accept currency as a per-deal property
        # on the create endpoint — it is a portal-level setting.
        # Sending it causes a 400 Bad Request.
    }

    # closedate is optional — only set it if the invoice carries a due_date.
    # HubSpot requires ISO 8601 format (YYYY-MM-DD); passing an empty string
    # or None causes a 400 validation error.
    due_date: str = invoice.get("due_date", "")
    if due_date:
        properties["closedate"] = due_date

    response = await client.post(
        _HUBSPOT_DEALS_URL,
        headers=_auth_headers(),
        json={"properties": properties},
        timeout=_DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    deal_id: str = response.json()["id"]
    return deal_id


async def push_invoice_to_crm(invoice: dict) -> str:
    """
    Push a validated invoice to HubSpot CRM as a Deal record.

    Implements a Search-then-Create pattern for idempotency:
        1. Search for an existing Deal whose ``dealname`` matches the canonical
           name built from ``invoice_id`` and ``vendor_name``.
        2. If found → return the existing Deal ID without making a write.
        3. If not found → create a new Deal and return its ID.

    This makes the function safe to call multiple times for the same invoice
    (e.g. when LangGraph retries ``integration_node`` after a partial failure)
    without creating duplicate deals in HubSpot.

    HubSpot field mappings:
        dealname      "{invoice_id} — {vendor_name}"
        amount        invoice["total_amount"] (float → string for HubSpot)
        dealstage     "appointmentscheduled" (first stage of the default pipeline)
        pipeline      "default"
        closedate     invoice["due_date"] (ISO 8601, omitted if not present)
        currency      invoice["currency"]

    Args:
        invoice: Validated invoice dict from the transform agent.  Expected
                 keys: ``invoice_id``, ``vendor_name``, ``total_amount``,
                 ``currency``.  ``due_date`` is optional.

    Returns:
        HubSpot Deal ID string (e.g. "12345678").  The caller (integration_agent)
        stores this as ``crm_record_id`` in the ``integration_results`` table.

    Raises:
        httpx.HTTPStatusError: On a non-2xx response from HubSpot (e.g. 401
            invalid token, 400 bad property value).  Propagated so the
            integration_agent can record a partial failure and the LangGraph
            error handler can retry the node.
    """
    deal_name = _build_deal_name(invoice)
    invoice_id: str = invoice.get("invoice_id", "unknown")

    async with httpx.AsyncClient() as client:
        # ── Step 1: Search for an existing deal ──────────────────────────────
        existing_id = await _search_existing_deal(client, deal_name)

        if existing_id is not None:
            logger.info(
                "api_tools: deal already exists in HubSpot — invoice_id='%s', "
                "deal_id=%s (idempotent retry).",
                invoice_id,
                existing_id,
            )
            return existing_id

        # ── Step 2: Create a new deal ─────────────────────────────────────────
        deal_id = await _create_deal(client, invoice, deal_name)

        logger.info(
            "api_tools: HubSpot Deal created — invoice_id='%s', deal_id=%s, "
            "dealname='%s'.",
            invoice_id,
            deal_id,
            deal_name,
        )
        return deal_id
