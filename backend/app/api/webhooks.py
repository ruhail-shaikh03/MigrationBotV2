"""Inbound Google Drive push notifications.

This is the third unauthenticated route in the application, alongside `/api/health` and
`/api/ready`, and the only one that causes a state change. It has to be: Drive posts to it
with no user credentials and no way to obtain any. Authentication is therefore of the
*sender*, via a shared secret handed to Drive at registration and echoed back on every
notification in `X-Goog-Channel-Token`, plus a channel ID that must match a channel we
actually opened.

What a notification does *not* contain is what changed. Drive watches a file; the payload
carries no cell, no range, not even a tab. So the handler does the only honest thing
available to it - marks every tab of that spreadsheet as needing a re-scan - and the actual
read happens later, on a real request, with a real user's OAuth token. No spreadsheet data
is ever fetched here, which is what keeps this endpoint safe to leave unauthenticated: the
worst a forged notification can achieve is making us re-read a sheet we already had.
"""

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy import select

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.models.sheet_watch_channel import SheetWatchChannel
from app.sheets.rows_cache import invalidate_tab

logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/drive", status_code=status.HTTP_200_OK)
async def drive_notification(
    x_goog_channel_id: Optional[str] = Header(None),
    x_goog_channel_token: Optional[str] = Header(None),
    x_goog_resource_state: Optional[str] = Header(None),
    x_goog_message_number: Optional[str] = Header(None),
):
    """Mark a watched spreadsheet's cached rows stale.

    Always answers quickly and does exactly two things - clear Redis, flag Postgres - because
    Drive treats a slow or failing endpoint as a reason to retry, and a handler that did real
    work here would turn one edit into a queue of duplicate scans.
    """
    configured = settings.DRIVE_WEBHOOK_TOKEN
    if not configured:
        # No token configured means we never registered a channel, so nothing legitimate can
        # be arriving. Refuse rather than accept an unauthenticated state change.
        logger.warning("Drive notification received but DRIVE_WEBHOOK_TOKEN is unset; refusing.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Drive push notifications are not enabled on this deployment.",
        )

    if not x_goog_channel_token or not hmac.compare_digest(x_goog_channel_token, configured):
        logger.warning(f"Drive notification with bad token on channel {x_goog_channel_id}.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid channel token.",
        )

    # Drive sends exactly one of these the moment a channel is opened. It means "the channel
    # works", not "the file changed" — acting on it would re-scan every tab of every
    # spreadsheet at registration time for no reason.
    if (x_goog_resource_state or "").lower() == "sync":
        return {"ok": True, "ignored": "sync"}

    if not x_goog_channel_id:
        return {"ok": True, "ignored": "no-channel-id"}

    try:
        async with AsyncSessionLocal() as db:
            channel = (
                await db.execute(
                    select(SheetWatchChannel).where(
                        SheetWatchChannel.channel_id == x_goog_channel_id
                    )
                )
            ).scalar_one_or_none()
    except Exception as e:
        logger.warning(f"Drive notification lookup failed for channel {x_goog_channel_id}: {e}")
        # Answer 200 regardless. Drive retries 5xx, and a database blip would turn into a
        # storm of redeliveries for a signal whose only consequence is a cache refresh.
        return {"ok": False, "error": "lookup-failed"}

    if channel is None:
        # A channel we replaced or never opened — most likely one we failed to stop, still
        # notifying until it expires. Not an error, and not ours to act on.
        return {"ok": True, "ignored": "unknown-channel"}

    # Whole spreadsheet: the notification cannot tell us which tab, and clearing all of them
    # is correct rather than merely convenient. invalidate_tab with no tab clears every Redis
    # key for the spreadsheet and flags every sheet_sync_state row dirty.
    await invalidate_tab(channel.spreadsheet_id)

    logger.info(
        f"Drive notification #{x_goog_message_number} ({x_goog_resource_state}) "
        f"invalidated {channel.spreadsheet_id}."
    )
    return {"ok": True, "spreadsheet_id": channel.spreadsheet_id}
