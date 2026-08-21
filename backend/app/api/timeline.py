"""The timeline read endpoint.

Separate from api/dashboard.py, which is already 680 lines — the same reason api/aliases.py
gives for not being a fourth resource inside api/admin.py, and the reason api/digest.py is
its own file. Everything here is composition: the computation lives in core/timeline.py,
which is pure and testable without a database, a Sheets client or a network.

Three details in the pipeline below are load-bearing:

* **`_filter_rows` is reused, never reimplemented.** It was extracted so the CSV export
  could not drift from the grid; a timeline that filtered differently from the grid beside
  it would be that defect a second time.
* **`_row_dicts` is called with `data_start_row`**, unlike api/digest.py, so every row
  carries its sheet row number. Without it a bar cannot open an editable row on a tracker
  whose IDs repeat — 27 of 412 rows on the reference sheet share one (§16.7).
* **No `limit` or `offset`.** The response is the whole filtered set, for the reason
  rows.csv has no paging: a chart that omits rows the count above it claims is the one
  thing a chart must not do.
"""

import logging
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard import _filter_rows, _resolve_project, _resolver_for, _row_dicts, _tab_for
from app.core.schema import get_available_tabs, get_tab_schema
from app.core.timeline import build_timeline
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth
from app.models.user import User
from app.sheets.client import build_sheets_service
from app.sheets.rows_cache import get_tab_matrix

logger = logging.getLogger("timeline")

router = APIRouter()


@router.get("/projects/{project_id}/timeline", response_model=Dict[str, Any])
async def project_timeline(
    project_id: int,
    tab: Optional[str] = None,
    q: Optional[str] = None,
    status: Optional[str] = None,
    person: Optional[str] = None,
    role_key: Optional[str] = None,
    overdue: bool = False,
    group_by: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """The tab's rows placed on a time axis, grouped, with the coverage that produced them.

    Gated as the other project reads are: `_resolve_project` answers 404 rather than 403,
    because telling an unauthorised caller that a project exists is itself a disclosure.
    """
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
    rows = _row_dicts(headers, raw_rows, data_start_row)
    resolver = await _resolver_for(db, project_id)

    filtered = await _filter_rows(
        db, project_id, rows, headers, tab_schema,
        q=q, status=status, person=person, role_key=role_key, overdue=overdue,
    )

    result = build_timeline(
        headers, filtered, tab_schema,
        group_by=group_by, resolver=resolver, today=date.today(),
        # Which columns can group this tab is a fact about the tab, not about the filter
        # in force. Handed the filtered set, `groupable` would drop the very column the
        # request grouped by whenever the surviving rows all leave it blank, and the
        # client's Group-by control would then match no option and show something else.
        all_rows=rows,
    )
    result["tab"] = active_tab
    result["tabs"] = get_available_tabs(project.schema_config or {})
    result["project_name"] = project.project_name
    result["primary_id_column"] = tab_schema.get("primary_id_column")
    # Surfaced, not swallowed: past _MAX_SCAN_ROWS every count above is a floor.
    result["truncated"] = truncated
    return result
