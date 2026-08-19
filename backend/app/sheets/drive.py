"""Google Drive calls, used purely as a change-detection signal for the row cache.

Sheets has no cell-change webhook. Drive has `files.watch`, which fires at *file*
granularity and whose payload never says which cell - or even which tab - changed. So
nothing here fetches spreadsheet data; these calls answer one question, "has this file
changed since we last looked", and the answer only ever decides whether to re-scan through
the normal Sheets read path as a real signed-in user.

Every function here returns None / False on failure rather than raising. A freshness check
that cannot complete must mean "assume stale", never "fail the request" - the fallback is a
live scan, which is exactly what this layer replaced and therefore always safe.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.config import settings
from app.sheets.retry import _with_retry

logger = logging.getLogger("sheets_drive")

# Drive caps channel lifetime; ask for a day and renew opportunistically well before it.
WATCH_TTL_SECONDS = 24 * 60 * 60
# Renew when a channel has less than this left, on any authenticated request that notices.
WATCH_RENEW_MARGIN_SECONDS = 6 * 60 * 60


def webhook_address() -> str:
    """The URL Drive posts notifications back to.

    One definition, used by both registration (`api/watch.py`) and renewal
    (`records_cache.py`). If these two ever disagreed, renewal would quietly repoint a
    working channel at an address nothing serves, and the only symptom would be caches that
    stopped being invalidated.
    """
    return settings.PUBLIC_BASE_URL.rstrip("/") + "/api/webhooks/drive"


def push_configured() -> bool:
    return bool(settings.PUBLIC_BASE_URL and settings.DRIVE_WEBHOOK_TOKEN)


async def get_modified_time(sheets_service: Any, spreadsheet_id: str) -> Optional[str]:
    """The file's RFC-3339 modifiedTime, or None if it cannot be determined.

    One cheap metadata call standing in for a full paginated scan of the tab. Treated as an
    opaque string - we only ever ask whether it differs from what we stored, never how much
    time elapsed.

    None is returned for every failure, including the 403 that users whose session predates
    the Drive scope will get until they sign in again. Callers read None as stale.
    """
    from app.sheets.client import drive_service_for

    drive = drive_service_for(sheets_service)
    if drive is None:
        return None
    try:
        meta = await _with_retry(lambda: drive.files().get(
            fileId=spreadsheet_id,
            fields="modifiedTime",
            supportsAllDrives=True,
        ).execute())
        value = (meta or {}).get("modifiedTime")
        return str(value) if value else None
    except Exception as e:
        logger.warning(f"Drive modifiedTime check failed for {spreadsheet_id}: {e}")
        return None


async def register_watch(
    drive_service: Any,
    spreadsheet_id: str,
    address: str,
    token: str,
    ttl_seconds: int = WATCH_TTL_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Open a push channel on the spreadsheet. Returns channel details, or None on failure.

    `address` must be an HTTPS URL on a domain verified in the Cloud Console project - Drive
    rejects the registration outright otherwise, which is the single most likely reason for
    this to return None on a first deployment.

    `token` is echoed back on every notification in `X-Goog-Channel-Token` and is what
    authenticates Drive to an endpoint that cannot carry user credentials.
    """
    channel_id = str(uuid.uuid4())
    expiration_ms = int(
        (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).timestamp() * 1000
    )
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": address,
        "token": token,
        "expiration": expiration_ms,
    }
    try:
        result = await _with_retry(lambda: drive_service.files().watch(
            fileId=spreadsheet_id,
            body=body,
            supportsAllDrives=True,
        ).execute())
    except Exception as e:
        logger.warning(f"Drive watch registration failed for {spreadsheet_id}: {e}")
        return None

    result = result or {}
    resource_id = result.get("resourceId")
    if not resource_id:
        logger.warning(f"Drive watch for {spreadsheet_id} returned no resourceId: {result}")
        return None

    # Drive may grant less than we asked for; trust its answer over our request.
    granted = result.get("expiration")
    try:
        expiration = datetime.fromtimestamp(int(granted) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        expiration = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    return {
        "channel_id": channel_id,
        "resource_id": str(resource_id),
        "token": token,
        "expiration": expiration,
    }


async def stop_watch(drive_service: Any, channel_id: str, resource_id: str) -> bool:
    """Close a push channel. False on failure - callers replace the row regardless.

    A channel we failed to stop keeps notifying until it expires; the handler ignores
    notifications for channels it has no row for, so a leaked channel is noise, not a fault.
    """
    try:
        await _with_retry(lambda: drive_service.channels().stop(
            body={"id": channel_id, "resourceId": resource_id}
        ).execute())
        return True
    except Exception as e:
        logger.warning(f"Drive watch stop failed for channel {channel_id}: {e}")
        return False
