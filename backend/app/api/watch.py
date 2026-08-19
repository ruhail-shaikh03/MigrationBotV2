"""Admin control over Drive push-notification channels.

Deliberately a separate module from `api/webhooks.py`. That one is unauthenticated by
necessity and only ever invalidates a cache; these endpoints are admin-gated and spend the
caller's own Google credentials. Keeping them in one file would put two opposite auth
postures a few lines apart, which is the kind of adjacency that eventually produces a route
missing its dependency.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import require_admin
from app.config import settings
from app.deps import get_current_user, get_db, get_google_auth
from app.models.project import Project
from app.models.sheet_watch_channel import SheetWatchChannel
from app.models.user import User
from app.sheets.client import build_drive_service
from app.sheets.drive import (
    WATCH_RENEW_MARGIN_SECONDS,
    register_watch,
    stop_watch,
    webhook_address,
)

logger = logging.getLogger("watch")

router = APIRouter(prefix="/watch", tags=["Watch"])


def _require_push_config() -> None:
    """Refuse clearly when push notifications are not configured.

    These settings intentionally have empty defaults so that a deployment missing them keeps
    working (falling back to `modifiedTime` polling) instead of crash-looping at import — see
    config.py. The cost of that choice is that the failure has to surface somewhere, and here
    is the right place: one endpoint returning an actionable message, rather than a channel
    registered against the address "/api/webhooks/drive" with no host.
    """
    missing = [
        name
        for name, value in (
            ("PUBLIC_BASE_URL", settings.PUBLIC_BASE_URL),
            ("DRIVE_WEBHOOK_TOKEN", settings.DRIVE_WEBHOOK_TOKEN),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Drive push notifications are not configured: {', '.join(missing)} unset. "
                "Set them in the deployment .env. Reads continue to work without them, "
                "using a modifiedTime check instead of push."
            ),
        )


async def _project_or_404(db: AsyncSession, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found."
        )
    return project


@router.get("/projects/{project_id}", dependencies=[Depends(require_admin)])
async def get_watch_status(
    project_id: int, db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """Whether this project has a live push channel, and when it lapses."""
    project = await _project_or_404(db, project_id)
    channel = (
        await db.execute(
            select(SheetWatchChannel).where(
                SheetWatchChannel.spreadsheet_id == project.spreadsheet_id
            )
        )
    ).scalar_one_or_none()

    if channel is None:
        return {
            "configured": bool(settings.PUBLIC_BASE_URL and settings.DRIVE_WEBHOOK_TOKEN),
            "active": False,
            "detail": "No channel registered; freshness falls back to a modifiedTime check.",
        }

    now = datetime.now(timezone.utc)
    return {
        "configured": bool(settings.PUBLIC_BASE_URL and settings.DRIVE_WEBHOOK_TOKEN),
        "active": channel.expiration > now,
        "expiration": channel.expiration.isoformat(),
        "expires_in_seconds": int((channel.expiration - now).total_seconds()),
        "needs_renewal": (channel.expiration - now).total_seconds()
        < WATCH_RENEW_MARGIN_SECONDS,
        "registered_by": channel.registered_by_email,
    }


@router.post("/projects/{project_id}", dependencies=[Depends(require_admin)])
async def register_project_watch(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    google_auth: dict = Depends(get_google_auth),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Open (or replace) the push channel for this project's spreadsheet.

    Registering replaces any existing channel rather than adding a second one: two live
    channels on one file means every edit arrives twice, and the second notification finds a
    cache that the first already invalidated. The old channel is stopped first on a
    best-effort basis - if Drive refuses, the stale channel keeps notifying until it expires
    and the handler ignores it, because its ID no longer matches any row.
    """
    _require_push_config()
    project = await _project_or_404(db, project_id)

    drive = build_drive_service(
        google_auth["access_token"], google_auth.get("refresh_token")
    )

    existing = (
        await db.execute(
            select(SheetWatchChannel).where(
                SheetWatchChannel.spreadsheet_id == project.spreadsheet_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await stop_watch(drive, existing.channel_id, existing.resource_id)

    result = await register_watch(
        drive,
        project.spreadsheet_id,
        webhook_address(),
        settings.DRIVE_WEBHOOK_TOKEN,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Google Drive refused the watch registration. The most common cause is that "
                f"the domain of {webhook_address()} is not verified as a push-notification "
                "domain in this project's Google Cloud Console. Reads are unaffected."
            ),
        )

    if existing is not None:
        await db.delete(existing)
        await db.flush()

    channel = SheetWatchChannel(
        spreadsheet_id=project.spreadsheet_id,
        channel_id=result["channel_id"],
        resource_id=result["resource_id"],
        token=result["token"],
        expiration=result["expiration"],
        registered_by_email=current_user.email,
    )
    db.add(channel)
    await db.commit()

    logger.info(
        f"Drive watch registered for {project.spreadsheet_id} by {current_user.email}, "
        f"expires {result['expiration'].isoformat()}."
    )
    return {
        "ok": True,
        "active": True,
        "expiration": result["expiration"].isoformat(),
        "address": webhook_address(),
    }


@router.delete("/projects/{project_id}", dependencies=[Depends(require_admin)])
async def stop_project_watch(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Close the push channel. Reads keep working, via the modifiedTime fallback."""
    project = await _project_or_404(db, project_id)
    channel = (
        await db.execute(
            select(SheetWatchChannel).where(
                SheetWatchChannel.spreadsheet_id == project.spreadsheet_id
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        return {"ok": True, "active": False, "detail": "No channel was registered."}

    drive = build_drive_service(
        google_auth["access_token"], google_auth.get("refresh_token")
    )
    stopped = await stop_watch(drive, channel.channel_id, channel.resource_id)

    await db.delete(channel)
    await db.commit()
    return {"ok": True, "active": False, "stopped_at_google": stopped}
