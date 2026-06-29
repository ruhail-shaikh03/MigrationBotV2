from typing import Optional
from fastapi import APIRouter, Depends
from app.deps import get_current_user
from app.models.user import User
from pydantic import BaseModel
from datetime import datetime

router = APIRouter(prefix="/auth", tags=["Auth"])

class UserProfileResponse(BaseModel):
    id: int
    email: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True

@router.get("/me")
async def get_current_profile(current_user: User = Depends(get_current_user)):
    """
    Returns the currently logged-in user profile, triggering auto-registration
    if this is their first visit.
    """
    return {
        "id": current_user.id,
        "email": current_user.email,
        "display_name": current_user.display_name,
        "avatar_url": current_user.avatar_url,
        "created_at": current_user.created_at,
        "last_login": current_user.last_login
    }
