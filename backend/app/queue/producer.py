import json
import uuid
import logging
from typing import Any
import redis.asyncio as aioredis
from app.config import settings
from app.queue.schemas import WriteJobPayload

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
    old_values: dict
) -> EnqueuedJob:
    """
    Serializes and pushes a write action request to the Redis queue.
    Generates a unique job ID to trace execution.
    """
    job_id = str(uuid.uuid4())
    
    payload = WriteJobPayload(
        user_email=user_email,
        google_access_token=google_access_token,
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
    
    return EnqueuedJob(job_id)
