"""
backend.tools.mock_notification_tools

Mock implementation of the Slack MCP notification tool.

Phase 4 replacement:
    This module is replaced by a live Slack MCP server integration in Phase 4.
    The MCP server will authenticate with the Slack API using a bot token,
    resolve the target channel by name or ID from settings, and post the
    message using Slack's chat.postMessage API. The MCP server returns Slack's
    native message timestamp ("ts") as the message identifier, which is used
    to thread replies and update messages in later workflow runs.

    The notification_agent.py import path stays the same — only this file is
    swapped. The function signature (message: str) -> str is preserved so the
    agent requires no changes.

Mock behaviour:
    Always succeeds. Logs the message being "sent" and returns a deterministic
    fake message ID based on a UUID. No actual HTTP request is made.
"""

import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)


async def send_slack_notification(message: str) -> str:
    """
    Mock Slack MCP tool — simulates posting a message to a Slack channel.

    Logs the message content and returns a fake Slack message ID. In Phase 4
    this is replaced by a live Slack MCP server call that posts to the
    configured channel via Slack's chat.postMessage API and returns the
    native Slack message timestamp ("ts") as the ID.

    Args:
        message: The plain-text or markdown-formatted message to send.

    Returns:
        A fake Slack message ID in the format "slack-msg-{uuid4}".
    """
    # Yield to the event loop — makes this mock behave like real async I/O
    # so any concurrent callers aren't blocked by a synchronous no-op.
    await asyncio.sleep(0)

    slack_message_id = f"slack-msg-{uuid.uuid4()}"

    logger.info(
        "mock_notification_tools: [MOCK] sending Slack notification:\n%s", message
    )
    logger.info(
        "mock_notification_tools: [MOCK] Slack post confirmed — slack_message_id=%s.",
        slack_message_id,
    )

    return slack_message_id
