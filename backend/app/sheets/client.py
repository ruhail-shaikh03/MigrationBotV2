from typing import Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings

# Our own attribute on our own object. googleapiclient's Resource keeps credentials behind
# `_http`, which is private and has changed shape across releases; stashing them ourselves
# under a namespaced name is the stable way to build a second client for the same user.
_CREDS_ATTR = "_migrationbot_credentials"


def _credentials(access_token: str, refresh_token: Optional[str] = None) -> Credentials:
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET
    )


def build_sheets_service(access_token: str, refresh_token: Optional[str] = None):
    """
    Build a Google Sheets API v4 discovery client using the user's Google OAuth tokens.
    Leverages client ID and secret settings from environment variables to allow token refresh.
    """
    creds = _credentials(access_token, refresh_token)
    # cache_discovery=False is recommended to prevent permission errors on some deployment environments
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    setattr(service, _CREDS_ATTR, creds)
    return service


def build_drive_service(access_token: str, refresh_token: Optional[str] = None):
    """A Drive v3 client for the same user.

    Drive is used for change detection only - `files.get(fields="modifiedTime")` and
    `files.watch` - never to read file content. That is why the frontend requests
    `drive.metadata.readonly` rather than `drive.readonly`: the narrower scope covers both
    calls, and the broader one would ask for read access to every file in the user's Drive
    to accomplish nothing extra.
    """
    return build("drive", "v3", credentials=_credentials(access_token, refresh_token),
                 cache_discovery=False)


def drive_service_for(sheets_service: Any):
    """A Drive client sharing an existing Sheets client's credentials, or None.

    `rows_cache.get_tab_matrix` is handed a Sheets service and nothing else, across all seven
    of its call sites. Rather than thread tokens through every one of them, the Drive client
    is derived from the Sheets client that was already built for this request.

    Returns None when the object carries no real credentials - which is the case for the
    MagicMock services the test suite passes, and for any caller that built its client some
    other way. None means "cannot determine freshness", and every caller treats that as stale
    and falls through to a live scan, so an unrecognised service degrades to today's behaviour
    rather than failing.
    """
    creds = getattr(sheets_service, _CREDS_ATTR, None)
    if not isinstance(creds, Credentials):
        return None
    return build("drive", "v3", credentials=creds, cache_discovery=False)
