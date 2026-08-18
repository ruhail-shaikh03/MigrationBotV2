"""Admin endpoints for the per-project person alias map.

Separate from api/admin.py, which is already long enough that adding a fourth resource to
it would make the file hard to hold in context.

Everything here is admin-only. Applying an alias changes how every user's dashboard reads
the sheet, which is a configuration decision, not a per-user preference.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import require_admin
from app.api.dashboard import _resolve_project, _row_dicts, _tab_for
from app.core.aliases import suggest_merges
from app.core.people import count_observed_names, normalise_person
from app.core.schema import bind_columns, get_people_columns, get_tab_schema
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth
from app.models.person_alias import PersonAlias
from app.models.user import User
from app.sheets.client import build_sheets_service
from app.sheets.rows_cache import get_tab_matrix

logger = logging.getLogger("aliases")

router = APIRouter()


class AliasCreate(BaseModel):
    alias: str
    canonical: str


@router.get("/projects/{project_id}/people", dependencies=[Depends(require_admin)])
async def list_people(
    project_id: int,
    tab: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """Names observed on the tab, merge suggestions, and the aliases already recorded.

    Counts are taken *after* splitting and *before* aliasing: the admin needs to see what
    the sheet actually says, including the variants they are about to merge away.
    """
    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, tab)
    tab_schema = get_tab_schema(project.schema_config or {}, active_tab)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    headers, raw_rows, _ = await get_tab_matrix(
        service, project.spreadsheet_id, active_tab, tab_schema.get("data_start_row", 3)
    )
    rows = _row_dicts(headers, raw_rows)

    people_columns = bind_columns(get_people_columns(tab_schema), headers)
    counts = count_observed_names(rows, people_columns)

    existing = await db.execute(select(PersonAlias).where(PersonAlias.project_id == project_id))
    aliases = existing.scalars().all()

    return {
        "tab": active_tab,
        "people_columns": people_columns,
        "names": [
            {"name": n, "count": c}
            for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        ],
        "suggestions": [
            {"name": s.name, "candidates": s.candidates, "reason": s.reason}
            for s in suggest_merges(counts)
        ],
        "aliases": [{"id": a.id, "alias": a.alias, "canonical": a.canonical} for a in aliases],
    }


@router.post("/projects/{project_id}/aliases", dependencies=[Depends(require_admin)])
async def create_alias(
    project_id: int,
    payload: AliasCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Record that one spelling means one person. Idempotent on (project, alias, canonical)."""
    await _resolve_project(db, current_user, project_id)

    alias_key = normalise_person(payload.alias)
    canonical = payload.canonical.strip()
    if not alias_key or not canonical:
        raise HTTPException(status_code=400, detail="Both alias and canonical are required.")
    if alias_key == normalise_person(canonical):
        raise HTTPException(status_code=400, detail="An alias cannot point at itself.")

    dup = await db.execute(
        select(PersonAlias).where(
            PersonAlias.project_id == project_id,
            PersonAlias.alias == alias_key,
            PersonAlias.canonical == canonical,
        )
    )
    row = dup.scalar()
    if row:
        return {"id": row.id, "status": "unchanged"}

    row = PersonAlias(
        project_id=project_id, alias=alias_key, canonical=canonical, created_by=current_user.id
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "status": "created"}


@router.delete("/projects/{project_id}/aliases/{alias_id}", dependencies=[Depends(require_admin)])
async def delete_alias(
    project_id: int,
    alias_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Undo a merge. Every alias decision has to be reversible — some of them will be wrong."""
    await _resolve_project(db, current_user, project_id)
    result = await db.execute(
        select(PersonAlias).where(PersonAlias.id == alias_id, PersonAlias.project_id == project_id)
    )
    row = result.scalar()
    if not row:
        raise HTTPException(status_code=404, detail="Alias not found.")
    await db.delete(row)
    await db.commit()
    return {"id": alias_id, "status": "deleted"}
