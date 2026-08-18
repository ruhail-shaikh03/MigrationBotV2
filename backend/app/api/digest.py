"""Preview of the per-person overdue digest.

**This endpoint computes and returns a digest. It does not send one.** There is no
scheduler here, no SMTP configuration, and no recipient list — deliberately, and the
absence is the feature. A digest leaves the system and lands in the inbox of someone who
never agreed to receive it, naming them and listing what they are late on; a wrong
recipient list or wrong wording cannot be quietly fixed in the next deploy, because it has
already been read.

So the computation is available to look at, and the decision to mail it to anybody stays
with a person who can see exactly what it says first.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard import _resolve_project, _resolver_for, _row_dicts, _tab_for
from app.core.digest import DEFAULT_LOOKAHEAD_DAYS, build_digest, render_text
from app.core.schema import get_tab_schema
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth
from app.models.user import User
from app.sheets.client import build_sheets_service
from app.sheets.rows_cache import get_tab_matrix

logger = logging.getLogger("digest")

router = APIRouter()


@router.get("/projects/{project_id}/digest", response_model=Dict[str, Any])
async def preview_digest(
    project_id: int,
    tab: Optional[str] = None,
    days: int = Query(DEFAULT_LOOKAHEAD_DAYS, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """What a digest for this tab would say today, plus its plain-text rendering.

    Gated as the other project reads are, not as admin-only: it reports on rows the caller
    can already see, grouped differently. Names are resolved through the project's alias
    map for the reason given in `core/digest.py` — listing one person twice under two
    spellings is the failure that makes a digest worse than nothing.
    """
    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, tab)
    tab_schema = get_tab_schema(project.schema_config or {}, active_tab)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    headers, raw_rows, truncated = await get_tab_matrix(
        service, project.spreadsheet_id, active_tab, tab_schema.get("data_start_row", 3)
    )
    rows = _row_dicts(headers, raw_rows)
    resolver = await _resolver_for(db, project_id)

    digest = build_digest(headers, rows, tab_schema, resolver=resolver, lookahead_days=days)
    digest["tab"] = active_tab
    digest["project_name"] = project.project_name
    digest["truncated"] = truncated
    digest["text"] = render_text(digest, project.project_name, active_tab)
    # Stated in the payload, not only in the docs, so a client that grows a "Send" button
    # has to notice there is nothing behind it yet.
    digest["delivery"] = "preview-only"
    return digest
