import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Optional
import redis.asyncio as aioredis
from app.config import settings
from app.queue.schemas import WriteJobPayload, JOB_STATE_PREFIX, JOB_STATE_TTL_SECONDS

logger = logging.getLogger("queue_producer")

# Initialize async redis client connecting to settings.REDIS_URL
redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


class EnqueuedJob:
    """Represents a job successfully inserted into the queue."""
    def __init__(self, job_id: str):
        self.id = job_id


async def enqueue_write_job(
    user_email: str,
    google_access_token: str,
    session_id: Any,
    tool_name: str,
    spreadsheet_id: str,
    sheet_tab: str,
    args: dict,
    old_values: dict,
    google_refresh_token: Optional[str] = None
) -> EnqueuedJob:
    """
    Serializes and pushes a write action request to the Redis queue.
    Generates a unique job ID to trace execution.
    """
    job_id = str(uuid.uuid4())

    payload = WriteJobPayload(
        user_email=user_email,
        google_access_token=google_access_token,
        google_refresh_token=google_refresh_token,
        session_id=session_id,
        tool_name=tool_name,
        spreadsheet_id=spreadsheet_id,
        sheet_tab=sheet_tab,
        args=args,
        old_values=old_values
    )

    envelope = {
        "job_id": job_id,
        "payload": payload.model_dump(mode="json")
    }

    queue_key = "migrationbot:write_queue"
    logger.info(f"Enqueuing write job {job_id} for tool {tool_name} to Redis.")

    # LPUSH/RPUSH to treat Redis list as a FIFO queue (RPUSH to enqueue, BLPOP/LPOP to dequeue)
    await redis_client.rpush(queue_key, json.dumps(envelope, ensure_ascii=False))

    # Makes job_id queryable (see api/jobs.py) from the moment it's returned to the
    # caller, not just once the worker picks it up.
    try:
        state = {
            "job_id": job_id,
            "status": "queued",
            "tool_name": tool_name,
            "user_email": user_email,
            "spreadsheet_id": spreadsheet_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await redis_client.set(
            f"{JOB_STATE_PREFIX}:{job_id}",
            json.dumps(state, ensure_ascii=False),
            ex=JOB_STATE_TTL_SECONDS
        )
    except Exception as e:
        logger.warning(f"Failed to set initial job state for {job_id}: {e}")

    return EnqueuedJob(job_id)
