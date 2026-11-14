"""Post a Discord message when a video task finishes or fails.

Set DISCORD_TASK_WEBHOOK_URL to a channel webhook (Channel settings >
Integrations > Webhooks). Unset means no messages. A failed post is logged
and never affects the task.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

WEBHOOK_ENV = "DISCORD_TASK_WEBHOOK_URL"
POST_TIMEOUT_SECONDS = 10
COLOR_DONE = 0x22C55E
COLOR_FAILED = 0xEF4444


def webhook_url() -> str:
    return os.getenv(WEBHOOK_ENV, "").strip()


def _task_link(app_base_url: str, task_id: str) -> str:
    return f"{app_base_url.rstrip('/')}/tasks/{task_id}"


def build_message(
    *,
    task_id: str,
    app_base_url: str,
    title: Optional[str],
    succeeded: bool,
    clips_count: int = 0,
    error: Optional[str] = None,
    queued_after: int = 0,
    minutes: Optional[float] = None,
) -> dict:
    """The webhook payload; kept separate so it can be tested without a network."""
    name = (title or "Your video").strip()[:200]
    if succeeded:
        heading = f"Clips ready: {name}"
        description = f"{clips_count} clip{'s' if clips_count != 1 else ''} made."
    else:
        heading = f"Clipping failed: {name}"
        description = (error or "Something went wrong.").strip()[:1000]
    fields = []
    if minutes is not None:
        fields.append({"name": "Took", "value": f"{minutes:.0f} min", "inline": True})
    if queued_after:
        fields.append({"name": "Still in queue", "value": str(queued_after), "inline": True})
    return {
        "username": "SupoClip",
        "embeds": [
            {
                "title": heading,
                "url": _task_link(app_base_url, task_id),
                "description": description,
                "color": COLOR_DONE if succeeded else COLOR_FAILED,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
        # Titles come from video sources; never let them ping anyone.
        "allowed_mentions": {"parse": []},
    }


async def notify_task_finished(**kwargs) -> None:
    """Send the message if a webhook is configured. Never raises."""
    url = webhook_url()
    if not url:
        return
    try:
        payload = build_message(**kwargs)
        async with httpx.AsyncClient(timeout=POST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            logger.warning(
                "Discord task notification for %s returned HTTP %s",
                kwargs.get("task_id"),
                response.status_code,
            )
    except Exception as error:
        # The webhook URL is a secret; log only the error type.
        logger.warning(
            "Could not send Discord task notification for %s (%s)",
            kwargs.get("task_id"),
            type(error).__name__,
        )
