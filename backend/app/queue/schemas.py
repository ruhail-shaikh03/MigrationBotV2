from typing import Optional, Dict, Any
from uuid import UUID
from pydantic import BaseModel, Field

# Shared between producer.py (sets initial state), worker.py (updates it through the
# job lifecycle), and api/jobs.py (reads it) — one definition so the three can't drift.
JOB_STATE_PREFIX = "migrationbot:job_state"
JOB_STATE_TTL_SECONDS = 7 * 24 * 3600

class WriteJobPayload(BaseModel):
    """
    Pydantic schema representing the write request enqueued to Redis.
    Preserves all execution context, including authorization credentials and audit trails.
    """
    user_email: str
    google_access_token: str = "mock-google-access-token"
    google_refresh_token: Optional[str] = None
    session_id: Optional[UUID] = None
    tool_name: str
    spreadsheet_id: str
    sheet_tab: str
    args: Dict[str, Any] = Field(default_factory=dict)
    old_values: Dict[str, Any] = Field(default_factory=dict)
