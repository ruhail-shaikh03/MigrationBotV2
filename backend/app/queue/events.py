import json
import logging
from typing import Any, Optional
import redis.asyncio as aioredis
from app.config import settings

logger = logging.getLogger("queue_events")

# The worker runs as its own container, so job status has to travel back to the API
# process (which owns the client WebSockets) over Redis pub/sub.
QUEUE_EVENTS_CHANNEL_PREFIX = "migrationbot:queue_events"

# Initialize async redis client connecting to settings.REDIS_URL
redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def queue_events_channel(user_email: str) -> str:
    """Per-user pub/sub channel name so events only reach the sockets of the originating user."""
    return f"{QUEUE_EVENTS_CHANNEL_PREFIX}:{(user_email or '').lower().strip()}"


async def publish_queue_update(
    user_email: str,
    job_id: str,
    status: str,
    tool_name: str,
    args: Optional[dict] = None,
    session_id: Any = None,
    error: Optional[str] = None,
) -> dict:
    """
    Publishes a 'queue_update' event describing the terminal state of a write job.
    Returns the event payload so callers can assert on it without reaching into Redis.
    """
    event = {
        "type": "queue_update",
        "job_id": job_id,
        "status": status,
        "tool_name": tool_name,
        "args": args or {},
        "session_id": str(session_id) if session_id else None,
        "error": error,
    }

    try:
        await redis_client.publish(
            queue_events_channel(user_email),
            json.dumps(event, ensure_ascii=False)
        )
        logger.info(f"Published queue_update for job {job_id}: {status}")
    except Exception as e:
        # Notification delivery must never fail the write job that already succeeded
        logger.warning(f"Failed to publish queue_update for job {job_id}: {e}")

    return event
