"""Read endpoints backing the project dashboard.

The chat surface answers questions about the sheet; these endpoints let the UI show the
sheet. Both read the same data, but the shapes differ: the agent wants a handful of
matched rows in prose-friendly form, while a grid wants the whole tab plus the column
metadata needed to render headers the sheet itself named (§7.4).

Access is gated exactly as `chat.py:list_user_projects` gates it — config admins see any
active project, everyone else needs an explicit `permissions` row. The fail-closed
default (§6.2) deliberately does not grant visibility here: a user who can only reach a
project through the default role was never actually granted anything.
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.people import collect_assignments, normalise_person, parse_date, resolve_effort
from app.core.permissions import get_user_permissions
from app.core.schema import (
    get_available_tabs, get_due_column, get_effort_columns, get_people_columns, get_tab_schema,
)
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth
from app.models.permission import Permission
from app.models.project import Project
from app.models.session import Session as UserSession
from app.models.user import User
from app.queue.producer import enqueue_write_job
from app.sheets.client import build_sheets_service
from app.sheets.read import get_row_raw
from app.sheets.rows_cache import get_tab_matrix

logger = logging.getLogger("dashboard")

router = APIRouter()


async def _resolve_project(db: AsyncSession, user: User, project_id: int) -> Project:
    """Fetch a project the caller is actually permitted to see, else 404.

    404 rather than 403 for a permission miss, matching `api/jobs.py`: telling an
    unauthorised caller that a project exists is itself a disclosure.
    """
    result = await db.execute(select(Project).where(Project.id == project_id, Project.is_active == True))  # noqa: E712
    project = result.scalar()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    if user.email.lower().strip() in settings.admin_emails_list:
        return project

    perm = await db.execute(
        select(Permission).where(Permission.project_id == project_id, Permission.user_id == user.id)
    )
    if not perm.scalar():
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _tab_for(project: Project, tab: Optional[str]) -> str:
    resolved = tab or project.default_tab
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="This project has no default tab; specify ?tab= explicitly.",
        )
    return resolved


def _row_dicts(headers: List[str], rows: List[List[str]]) -> List[Dict[str, str]]:
    """Zip the raw matrix into per-row dicts keyed by the sheet's own headers.

    Rows come back ragged — Sheets omits trailing empty cells — so short rows are padded
    rather than truncating the header list, which would drop real columns from the grid.
    """
    out = []
    for row in rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        out.append({h: padded[i] for i, h in enumerate(headers) if h})
    return out


def _column_descriptors(
    tab_schema: dict,
    headers: List[str],
    rows: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """What the grid should render, in sheet order, with roles marked.

    The frontend never decides which columns are people — it is told. That is the whole
    point of §7.4: a sheet naming a "QA Owner" gets a QA Owner column with no code change.

    `has_data` reports whether the column holds a value in *any* row of the tab. Real
    trackers carry a long tail of untouched columns — the reference sheet has a dozen
    literally named "Column 15", "Column 16" and so on — and rendering them pushes the
    columns that matter off-screen behind a horizontal scrollbar. Emptiness is measured
    over the whole scan, not the current page, so paging never changes which columns
    appear; the client hides them by default and can reveal them.
    """
    people = get_people_columns(tab_schema)
    effort = get_effort_columns(tab_schema)
    people_by_norm = {p["header"].lower().strip(): p for p in people}
    effort_by_norm = {e["header"].lower().strip(): e for e in effort}

    populated = set()
    if rows is not None:
        for row in rows:
            for header, value in row.items():
                if str(value).strip():
                    populated.add(header)

    descriptors = []
    for h in headers:
        if not h:
            continue
        norm = h.lower().strip()
        person = people_by_norm.get(norm)
        eff = effort_by_norm.get(norm)
        descriptors.append({
            "header": h,
            "label": (person or eff or {}).get("label", h),
            "key": (person or eff or {}).get("key"),
            "kind": "person" if person else ("effort" if eff else "plain"),
            "has_data": True if rows is None else h in populated,
        })
    return descriptors


def _matches(row: Dict[str, str], header: Optional[str], needle: str) -> bool:
    if not header or not needle:
        return True
    return needle.lower().strip() in str(row.get(header, "")).lower()


@router.get("/projects/{project_id}/rows", response_model=Dict[str, Any])
async def list_rows(
    project_id: int,
    tab: Optional[str] = None,
    q: Optional[str] = None,
    status: Optional[str] = None,
    person: Optional[str] = None,
    role_key: Optional[str] = None,
    overdue: bool = False,
    sort: Optional[str] = None,
    desc: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """The tab's rows, filtered and paged, plus the column metadata to render them."""
    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, tab)
    tab_schema = get_tab_schema(project.schema_config or {}, active_tab)
    data_start_row = tab_schema.get("data_start_row", 3)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    headers, raw_rows, truncated = await get_tab_matrix(
        service, project.spreadsheet_id, active_tab, data_start_row
    )
    rows = _row_dicts(headers, raw_rows)

    people = get_people_columns(tab_schema)
    status_header = tab_schema.get("status_column")
    # Resolved against the real headers, so a sheet whose deadline column detection never
    # mapped ("Expected Completetion Date") still filters by overdue instead of matching
    # nothing at all. Same resolver the agent's summarize(overdue) uses.
    due_header = get_due_column(tab_schema, headers)

    # Only the people-columns the caller asked about; role_key narrows a person filter to
    # one role so "overdue work Sara owns as functional consultant" is answerable.
    scoped_people = [p for p in people if p["key"] == role_key] if role_key else people
    person_norm = normalise_person(person) if person else ""

    filtered = []
    for row in rows:
        if q and not any(q.lower().strip() in str(v).lower() for v in row.values()):
            continue
        if status and not _matches(row, status_header, status):
            continue
        if person_norm:
            names = {normalise_person(row.get(p["header"], "")) for p in scoped_people}
            if person_norm not in names:
                continue
        if overdue:
            due = parse_date(row.get(due_header)) if due_header else None
            if not due or not _is_overdue(due, row, status_header):
                continue
        filtered.append(row)

    if sort and sort in headers:
        filtered.sort(key=lambda r: str(r.get(sort, "")).lower(), reverse=desc)

    total = len(filtered)
    page = filtered[offset:offset + limit]

    return {
        "tab": active_tab,
        # Every tab the project has, so the grid can offer a switcher. Without it the
        # client had no way to learn a tab existed, and `?tab=` — which the endpoint has
        # always honoured — was never sent, pinning the dashboard to `default_tab`.
        "tabs": get_available_tabs(project.schema_config or {}),
        "project_name": project.project_name,
        # Emptiness is judged across every scanned row, not the page being returned, so
        # which columns show up does not change as the user pages through.
        "columns": _column_descriptors(tab_schema, headers, rows),
        "people_columns": people,
        # The grid needs this to address a row when queueing an edit — without it the
        # client would have to guess which column is the identifier.
        "primary_id_column": tab_schema.get("primary_id_column"),
        "rows": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        # Surfaced, not swallowed: past _MAX_SCAN_ROWS the totals below are a floor, and a
        # dashboard that silently under-reports is worse than one that says it is partial.
        "truncated": truncated,
    }


def _is_overdue(due: date, row: Dict[str, str], status_header: Optional[str]) -> bool:
    """Past its due date and not in a status that reads as finished.

    "Finished" is matched against generic words rather than a configured allowlist,
    because no sheet declares which of its statuses mean done. That makes this a
    heuristic, and it is applied only to the `overdue` filter — never to a write.

    The word list is kept narrow on purpose. Hiding a genuinely overdue item is the worse
    error here — nobody chases what they cannot see — while an extra row in the list is
    self-correcting. So "live" is deliberately absent: it would clear "Go-Live Pending".
    """
    if due >= date.today():
        return False
    if status_header:
        value = str(row.get(status_header, "")).lower().strip()
        if any(word in value for word in ("done", "complete", "closed", "cancel", "deployed")):
            return False
    return True


class RowUpdate(BaseModel):
    field: str
    value: str


class RowPatch(BaseModel):
    tab: Optional[str] = None
    updates: List[RowUpdate]


@router.patch("/projects/{project_id}/rows/{row_id}", response_model=Dict[str, Any])
async def patch_row(
    project_id: int,
    row_id: str,
    payload: RowPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Queue a cell edit made from the dashboard.

    RBAC is normally enforced inside `agentic_loop.py`, immediately before tool dispatch.
    A REST route does not pass through that loop, so it must run the identical check
    itself — a write path that skips `PermissionChecker` is a privilege-escalation bug,
    not a missing feature. The `args` shape below is therefore not incidental: the
    field-level gate reads `args["updates"][*]["field"]` (`core/permissions.py`), so
    passing a differently-shaped dict would silently disable field restrictions while
    still appearing to check permissions.

    Everything after the check is the existing write path unchanged — the same producer,
    the same queue, the same worker — so throttling, audit logging and cache invalidation
    all apply exactly as they do for a write the model made.
    """
    if not payload.updates:
        raise HTTPException(status_code=400, detail="No updates supplied.")

    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, payload.tab)

    updates = [{"field": u.field, "value": u.value} for u in payload.updates]
    args = {"ricefw_id": row_id, "updates": updates}

    checker = await get_user_permissions(db, current_user.email, project_id)
    allowed, reason = checker.can_execute("update_cell", args)
    if not allowed:
        # 403 with the checker's own explanation: unlike the read path, the caller
        # already knows this project and row exist, so there is nothing to conceal and a
        # 404 here would just be confusing.
        raise HTTPException(status_code=403, detail=reason)

    # One Session row per (user, project), matching chat.py — the worker records it on the
    # audit entry, so a dashboard edit is attributable to the same session as a chat edit.
    sess_res = await db.execute(
        select(UserSession).where(
            UserSession.user_id == current_user.id, UserSession.project_id == project.id
        )
    )
    user_sess = sess_res.scalar()
    if not user_sess:
        user_sess = UserSession(
            user_id=current_user.id, project_id=project.id, active_tab=active_tab
        )
        db.add(user_sess)
        await db.commit()
        await db.refresh(user_sess)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )

    # Pre-read so the audit row carries a real before-value. Best-effort, exactly as
    # tool_dispatch does it: failing to read the old value must not block the write, since
    # a missing audit diff is a smaller loss than a refused edit.
    old_values: Dict[str, str] = {}
    try:
        old_values = await get_row_raw(
            project.spreadsheet_id,
            active_tab,
            row_id,
            [u["field"] for u in updates],
            project.schema_config or {},
            service,
        )
    except Exception as e:
        logger.warning(f"Failed to pre-read row {row_id} for audit: {e}")

    job = await enqueue_write_job(
        user_email=current_user.email,
        google_access_token=google_auth["access_token"],
        google_refresh_token=google_auth.get("refresh_token"),
        session_id=user_sess.id,
        tool_name="update_cell",
        spreadsheet_id=project.spreadsheet_id,
        sheet_tab=active_tab,
        args=args,
        old_values=old_values,
    )

    return {"ok": True, "job_id": job.id, "status": "queued", "tab": active_tab, "row_id": row_id}


@router.get("/projects/{project_id}/analytics", response_model=Dict[str, Any])
async def project_analytics(
    project_id: int,
    tab: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Workload per person (split by role), overdue counts, and a status breakdown."""
    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, tab)
    tab_schema = get_tab_schema(project.schema_config or {}, active_tab)
    data_start_row = tab_schema.get("data_start_row", 3)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    headers, raw_rows, truncated = await get_tab_matrix(
        service, project.spreadsheet_id, active_tab, data_start_row
    )
    rows = _row_dicts(headers, raw_rows)

    people = get_people_columns(tab_schema)
    effort = get_effort_columns(tab_schema)
    status_header = tab_schema.get("status_column")
    date_columns = tab_schema.get("date_columns") or {}
    due_header = get_due_column(tab_schema, headers)

    by_status: Dict[str, int] = {}
    workload: Dict[str, Dict[str, Any]] = {}
    effort_sources = set()
    effort_rows = 0

    for row in rows:
        if status_header:
            label = str(row.get(status_header, "")).strip() or "(blank)"
            by_status[label] = by_status.get(label, 0) + 1

        days, source = resolve_effort(row, effort, date_columns)
        if source:
            effort_sources.add(source)
            effort_rows += 1

        due = parse_date(row.get(due_header)) if due_header else None
        is_overdue = bool(due) and _is_overdue(due, row, status_header)

        for assignment in collect_assignments(row, people):
            entry = workload.setdefault(assignment["person_key"], {
                "person": assignment["person"],
                "total_items": 0,
                "overdue_items": 0,
                "total_days": 0.0,
                "roles": {},
            })
            entry["total_items"] += 1
            if is_overdue:
                entry["overdue_items"] += 1
            if days is not None:
                entry["total_days"] += days
            role = entry["roles"].setdefault(
                assignment["role_key"], {"label": assignment["role_label"], "items": 0}
            )
            role["items"] += 1

    # One source wins so the UI can label the metric honestly. Mixed sources across rows
    # would make a column of days that means different things per row, so "column" is
    # reported only when nothing had to be derived from dates.
    days_source = None
    if effort_sources == {"column"}:
        days_source = "column"
    elif effort_sources:
        days_source = "mixed" if len(effort_sources) > 1 else "dates"

    ranked = sorted(workload.values(), key=lambda w: (-w["total_items"], w["person"].lower()))

    return {
        "tab": active_tab,
        "total_rows": len(rows),
        "by_status": [{"label": k, "count": v} for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])],
        "workload": ranked,
        "people_columns": people,
        # None means the sheet records no effort anywhere: the UI must show item counts
        # and say so rather than render a zero-day bar, which asserts something false.
        "days_source": days_source,
        # How many rows actually yielded a day figure. On the reference sheet only ~40 of
        # 412 rows carry both dates, so a bare "525 days" reads as a project total when it
        # is a sum over a tenth of the work. The UI states the denominator instead.
        "effort_rows": effort_rows,
        "has_due_dates": bool(due_header),
        "truncated": truncated,
    }
