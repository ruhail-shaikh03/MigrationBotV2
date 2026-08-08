import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from app.deps import get_current_user
from app.models.user import User
from app.config import settings
from app.queue.producer import redis_client
from app.queue.schemas import JOB_STATE_PREFIX

logger = logging.getLogger("api_jobs")
router = APIRouter(tags=["Jobs"])


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, current_user: User = Depends(get_current_user)):
    """Query the state of a queued write job by the job_id dispatch_tool already
    returns to the caller — makes that id actually useful instead of a value nobody
    can do anything with after the fact. Scoped to the job's own user or a config
    admin, since state includes tool_name/args."""
    raw = await redis_client.get(f"{JOB_STATE_PREFIX}:{job_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail="Job not found — it may have expired or never existed.")

    state = json.loads(raw)

    email_clean = current_user.email.lower().strip()
    is_admin = email_clean in settings.admin_emails_list
    job_owner = (state.get("user_email") or "").lower().strip()
    if not is_admin and job_owner != email_clean:
        # 404, not 403 — don't confirm a job_id exists to a caller who doesn't own it.
        raise HTTPException(status_code=404, detail="Job not found — it may have expired or never existed.")

    return state
