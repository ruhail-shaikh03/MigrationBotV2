from typing import Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from app.config import settings

def build_sheets_service(access_token: str, refresh_token: Optional[str] = None):
    """
    Build a Google Sheets API v4 discovery client using the user's Google OAuth tokens.
    Leverages client ID and secret settings from environment variables to allow token refresh.
    """
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET
    )
    # cache_discovery=False is recommended to prevent permission errors on some deployment environments
    return build("sheets", "v4", credentials=creds, cache_discovery=False)
