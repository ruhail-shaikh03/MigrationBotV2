from datetime import datetime, timezone
from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.db.engine import get_db
from app.models.user import User

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Decodes the NextAuth JWT to fetch or create the user in the database.
    Supports a 'mock-' prefix fallback for developer CLI testing and unit tests.
    """
    token = credentials.credentials
    try:
        # Standard decode using NextAuth JWT Secret
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        email: str = payload.get("email")
        if email is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication token is missing the email address claim.",
            )
    except JWTError:
        # Fallback helper for local manual testing/curl/mock tests
        if token.startswith("mock-") or "@" in token:
            email = token.replace("mock-", "")
            payload = {
                "email": email,
                "name": email.split("@")[0].replace(".", " ").title(),
                "picture": None,
                "sub": f"mock-sub-{email}"
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate signature of the authentication token.",
            )

    email_clean = email.lower().strip()
    
    # Check if user exists in PostgreSQL
    result = await db.execute(select(User).where(User.email == email_clean))
    user = result.scalar()
    
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if not user:
        # First login: dynamically provision user account (Auto-registration)
        user = User(
            email=email_clean,
            display_name=payload.get("name"),
            avatar_url=payload.get("picture"),
            google_sub=payload.get("sub"),
            last_login=now_utc
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # Existing user: update last login timestamp
        user.last_login = now_utc
        await db.commit()
        
    return user


async def get_google_token(
    x_google_access_token: Optional[str] = Header(None, alias="X-Google-Access-Token")
) -> str:
    """
    Retrieves the Google OAuth Access Token sent from the Next.js frontend.
    If none is provided (development context), returns a mock string.
    """
    if not x_google_access_token:
        # Developer fallback
        return "mock-google-access-token"
    return x_google_access_token
