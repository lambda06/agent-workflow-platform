"""
backend.tools.notification_tools

Real Slack implementation of the workflow notification tool.

Replaces backend.tools.mock_notification_tools, which always succeeded with a
fake UUID-based message ID.  The function signature and return semantics are
identical — notification_agent.py requires no changes beyond updating its
import path.

Design notes
------------
Client lifetime
    ``_slack_client`` is created once at module import time, not inside
    ``send_slack_notification``.  ``AsyncWebClient`` owns an ``aiohttp``
    ``ClientSession`` internally; constructing it on every call would open and
    immediately orphan a session on each invocation, leaking file descriptors
    and defeating connection-pool reuse.  Module-level instantiation matches
    the pattern recommended in the slack_sdk documentation.

Message ID (``ts``)
    Slack's ``chat.postMessage`` response includes a ``ts`` (timestamp) field
    of the form ``"1712345678.123456"``.  This value is Slack's canonical,
    channel-scoped message identifier: it is the primary key used by
    ``chat.update``, ``chat.delete``, ``reactions.add``, and threaded replies
    (``thread_ts``).  Returning ``ts`` therefore lets any downstream agent
    update, delete, or thread off the posted message without needing an
    additional lookup.
"""

import logging

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from backend.config.settings import settings

logger = logging.getLogger(__name__)

# Instantiated once at module level to reuse the underlying aiohttp session
# across all calls. See module docstring for rationale.
_slack_client = AsyncWebClient(token=settings.slack_bot_token)


async def send_slack_notification(message: str) -> str:
    """
    Post a plain-text or markdown-formatted message to the configured Slack
    channel and return the Slack message timestamp as a stable message ID.

    Args:
        message: The plain-text or mrkdwn-formatted message body to post.

    Returns:
        The Slack message timestamp (``ts``) string, e.g.
        ``"1712345678.123456"``, which serves as the unique message ID for
        this channel.

    Raises:
        SlackApiError: Propagated unchanged after logging when the Slack API
            returns a non-OK response (e.g. ``invalid_auth``,
            ``channel_not_found``, ``not_in_channel``).
    """
    logger.info(
        "notification_tools: posting Slack notification to channel=%s",
        settings.slack_channel_id,
    )

    try:
        response = await _slack_client.chat_postMessage(
            channel=settings.slack_channel_id,
            text=message,
        )
    except SlackApiError as exc:
        logger.error(
            "notification_tools: Slack API error posting to channel=%s — %s",
            settings.slack_channel_id,
            exc.response["error"],
        )
        raise

    ts: str = response["ts"]
    logger.info(
        "notification_tools: Slack post confirmed — channel=%s ts=%s",
        settings.slack_channel_id,
        ts,
    )
    return ts
