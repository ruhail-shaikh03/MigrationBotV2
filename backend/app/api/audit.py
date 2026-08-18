"""Reverting a write that has already been applied.

`audit_logs` has recorded `old_value` and `new_value` on every mutation since Phase 1, and
until now nothing read them back. The whole undo is therefore a lookup and an inverse
write: no new state, no new table, and no new way for a cell to change.

That inverse write goes through the ordinary queue — the same producer, the same worker, at
the same 1 req/sec — so it is itself throttled, itself audited, and it invalidates the row
cache exactly like any other edit. An undo is a write, and treating it as anything more
special than that is how a second write path gets built by accident (§6.1).

Two limits are deliberate and are not oversights:

* **`update_cell` and `bulk_update` only.** Undoing an `add_row` means deleting a row, and
  nothing else in this system deletes anything. `format_row` records a synthetic
  before-value that was never a real cell.
* **The cell must still hold the value that was written.** See `_verify_unchanged`.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permissions import get_user_permissions
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth
from app.models.audit_log import AuditLog
from app.models.project import Project
from app.models.session import Session as UserSession
from app.models.user import User
from app.queue.producer import enqueue_write_job
from app.sheets.client import build_sheets_service
from app.sheets.read import get_row_raw

logger = logging.getLogger("audit")

router = APIRouter()

# Tools whose audit rows describe a single cell going from one value to another, which is
# the only thing that can be inverted by writing a value back.
UNDOABLE_TOOLS = ("update_cell", "bulk_update")


def _normalise(value: Optional[str]) -> str:
    """Compare cell values the way a sheet means them, not the way Python does.

    Sheets returns trailing whitespace and empty cells inconsistently, and `None` from the
    audit row means the cell was blank — which is the same thing the sheet reports as `""`.
    Treating those as different would refuse a perfectly safe undo of a write that filled
    in a blank.
    """
    return str(value or "").strip()


@router.post("/audit/{audit_id}/undo", response_model=Dict[str, Any])
async def undo_write(
    audit_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Queue the inverse of one audited write.

    The ordering mirrors `dashboard.py:patch_row` exactly, for the reason given there: a
    write path that skips `PermissionChecker` is a privilege-escalation bug, and the `args`
    shape is load-bearing because the field-level gate reads `args["updates"][*]["field"]`.
    """
    result = await db.execute(select(AuditLog).where(AuditLog.id == audit_id))
    entry = result.scalar()
    if not entry:
        raise HTTPException(status_code=404, detail="No such audit entry.")

    email = current_user.email.lower().strip()
    is_admin = email in settings.admin_emails_list
    if not is_admin and (entry.user_email or "").lower().strip() != email:
        # 404 rather than 403, matching api/jobs.py: confirming that someone else's write
        # exists is itself a disclosure. The permission check below still applies to the
        # owner — owning a write does not grant the right to write that field again.
        raise HTTPException(status_code=404, detail="No such audit entry.")

    return await _undo_entry(entry, db, current_user, google_auth)


class UndoCellRequest(BaseModel):
    project_id: int
    tab: str
    ricefw_id: str
    field: str


@router.post("/audit/undo-cell", response_model=Dict[str, Any])
async def undo_latest_for_cell(
    payload: UndoCellRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Undo the caller's most recent successful write to one cell.

    This exists because the chat write ledger — where someone notices a mistake seconds
    after making it, and the single most likely place an undo is wanted — cannot name an
    audit row. `audit_logs` records no job id, and the ledger is keyed by job id; there is
    no column joining them, and adding one is not possible without migration tooling
    (`create_all` brings up new tables but never alters existing ones, §15.2).

    Resolving by cell rather than by id is also the more honest operation for that surface.
    The ledger entry says "W-1 · Dev Status → Completed"; the thing the user means by Undo
    is "put that cell back", and if they have since changed it twice, the latest write is
    the one they mean. Everything after resolution is the same code path as undoing by id,
    including the guard that refuses when someone else moved the cell.
    """
    project_result = await db.execute(
        select(Project).where(Project.id == payload.project_id, Project.is_active == True)  # noqa: E712
    )
    project = project_result.scalar()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    query = (
        select(AuditLog)
        .where(
            AuditLog.spreadsheet_id == project.spreadsheet_id,
            AuditLog.sheet_tab == payload.tab,
            AuditLog.ricefw_id == payload.ricefw_id,
            AuditLog.field == payload.field,
            AuditLog.result_ok == True,  # noqa: E712
            AuditLog.tool_name.in_(UNDOABLE_TOOLS),
        )
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .limit(1)
    )
    email = current_user.email.lower().strip()
    if email not in settings.admin_emails_list:
        # Scoped to the caller's own writes, so "undo" here can never reach past someone
        # else's edit to find an older one of yours underneath it.
        query = query.where(AuditLog.user_email == email)

    entry = (await db.execute(query)).scalar()
    if not entry:
        raise HTTPException(
            status_code=404,
            detail=f"No write of yours to {payload.ricefw_id} · {payload.field} to undo.",
        )

    return await _undo_entry(entry, db, current_user, google_auth)


async def _undo_entry(
    entry: AuditLog, db: AsyncSession, current_user: User, google_auth: dict
) -> Dict[str, Any]:
    """Queue the inverse of one audit entry. Shared by both routes above."""
    if entry.tool_name not in UNDOABLE_TOOLS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{entry.tool_name} cannot be undone automatically. "
                "Undoing an added row would mean deleting it, which this app never does."
            ),
        )
    if not entry.result_ok:
        # Nothing landed, so there is nothing to put back — and reverting to `old_value`
        # here would *write* a value the sheet never had, dressed up as a correction.
        raise HTTPException(status_code=400, detail="That write failed, so there is nothing to undo.")
    if not entry.ricefw_id or not entry.field:
        raise HTTPException(status_code=400, detail="That entry does not identify a single cell.")

    project_result = await db.execute(
        select(Project).where(
            Project.spreadsheet_id == entry.spreadsheet_id,
            Project.is_active == True,  # noqa: E712
        )
    )
    project = project_result.scalar()
    if not project:
        raise HTTPException(
            status_code=404, detail="The project this write belonged to is no longer active."
        )

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    await _verify_unchanged(entry, project, service)

    updates = [{"field": entry.field, "value": entry.old_value or ""}]
    args: Dict[str, Any] = {
        "ricefw_id": entry.ricefw_id,
        "updates": updates,
        # Carried into args_json so the reversal is legible in the audit trail as a
        # reversal, rather than as an unexplained second edit to the same cell. The worker
        # reads only `ricefw_id` and `updates`, so the extra key is inert.
        "undo_of": entry.id,
    }

    checker = await get_user_permissions(db, current_user.email, project.id)
    allowed, reason = checker.can_execute("update_cell", args)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)

    sess_res = await db.execute(
        select(UserSession).where(
            UserSession.user_id == current_user.id, UserSession.project_id == project.id
        )
    )
    user_sess = sess_res.scalar()
    if not user_sess:
        user_sess = UserSession(
            user_id=current_user.id, project_id=project.id, active_tab=entry.sheet_tab
        )
        db.add(user_sess)
        await db.commit()
        await db.refresh(user_sess)

    job = await enqueue_write_job(
        user_email=current_user.email,
        google_access_token=google_auth["access_token"],
        google_refresh_token=google_auth.get("refresh_token"),
        session_id=user_sess.id,
        tool_name="update_cell",
        spreadsheet_id=entry.spreadsheet_id,
        sheet_tab=entry.sheet_tab,
        args=args,
        # The value being replaced is the one this entry wrote — already confirmed to be
        # what the cell still holds, so the new audit row's diff is accurate rather than
        # assumed.
        old_values={entry.field: entry.new_value or ""},
    )

    return {
        "ok": True,
        "job_id": job.id,
        "status": "queued",
        "undo_of": entry.id,
        "ricefw_id": entry.ricefw_id,
        "field": entry.field,
        "restoring": entry.old_value or "",
    }


async def _verify_unchanged(entry: AuditLog, project: Project, service: Any) -> None:
    """Refuse the undo unless the cell still holds the value this entry wrote.

    This is the point of the endpoint being a read-then-write rather than a straight
    enqueue. An audit row is a record of what happened *then*; between then and now someone
    else may have edited the same cell, and blindly restoring `old_value` would silently
    destroy their edit while reporting success. The person undoing would never learn of it,
    and neither would the person overwritten — the audit trail would show a legitimate
    correction.

    A read failure refuses too. A guard that fails open is not a guard, and the cost of
    refusing is that someone retries in a minute.
    """
    try:
        current = await get_row_raw(
            project.spreadsheet_id,
            entry.sheet_tab,
            entry.ricefw_id,
            [entry.field],
            project.schema_config or {},
            service,
        )
    except Exception as e:
        logger.warning(f"Could not verify cell before undo of audit {entry.id}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Could not read the current value to check it is safe to undo. Try again shortly.",
        )

    actual = _normalise(current.get(entry.field))
    written = _normalise(entry.new_value)
    if actual != written:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{entry.ricefw_id} · {entry.field} now reads "
                f"\"{actual or '(blank)'}\", not \"{written or '(blank)'}\". "
                "Someone changed it after this write, so undoing would discard their edit."
            ),
        )
