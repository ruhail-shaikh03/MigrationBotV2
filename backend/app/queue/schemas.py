from typing import Optional, Dict, Any
from uuid import UUID
from pydantic import BaseModel, Field

class WriteJobPayload(BaseModel):
    """
    Pydantic schema representing the write request enqueued to Redis.
    Preserves all execution context, including authorization credentials and audit trails.
    """
    user_email: str
    google_access_token: str = "mock-google-access-token"
    session_id: Optional[UUID] = None
    tool_name: str
    spreadsheet_id: str
    sheet_tab: str
    args: Dict[str, Any] = Field(default_factory=dict)
    old_values: Dict[str, Any] = Field(default_factory=dict)
