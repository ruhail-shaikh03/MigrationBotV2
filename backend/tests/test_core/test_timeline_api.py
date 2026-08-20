"""The timeline endpoint.

Composition only — the arithmetic is tested in test_timeline.py. What matters here is
that this route inherits the read pipeline rather than reimplementing it:

  * the same visibility rule as every other project read, which answers 404 and not 403,
    because confirming a project exists is itself a disclosure;
  * `_filter_rows`, so the chart cannot drift from the grid beside it — the reason that
    helper was extracted for the CSV export in the first place;
  * `_row_dicts` **with** data_start_row, unlike api/digest.py, so each row carries its
    sheet row number and a bar can open an editable row on a tracker with duplicated IDs.

TestClient is constructed without its context manager, matching test_webhooks.py, so
FastAPI's lifespan — and therefore init_db()'s connection attempt — never runs.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_google_auth
from app.db.engine import get_db
from app.main import app

ENDPOINT = "/api/projects/1/timeline"

HEADERS = ["WRICEF No.", "Description", "Module", "Dev Status", "Start Date",
           "Expected Completetion Date", "Developer Name"]

MATRIX_ROWS = [
    ["W-1", "Migrate BOM report", "SD", "In Progress", "01/02/2026", "09/02/2026", "Sara Iqbal"],
    ["W-2", "Rebuild pricing exit", "MM", "Not Started", "", "28/02/2026", "Ahmed Qamar"],
    ["W-3", "Archive old IDocs", "SD", "Completed", "", "", ""],
]

PROJECT = SimpleNamespace(
    id=1,
    project_name="HEDP Tracker",
    spreadsheet_id="sheet-123",
    default_tab="SD",
    schema_config={
        "primary_id_column": "WRICEF No.",
        "description_column": "Description",
        "module_column": "Module",
        "status_column": "Dev Status",
        "date_columns": {"start": "Start Date", "due": "Expected Completetion Date"},
        "people_columns": [{"key": "dev", "label": "Developer", "header": "Developer Name"}],
        "data_start_row": 3,
    },
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _auth():
    """The route's three injected dependencies, faked. `_resolve_project` is patched per
    test, so the database handle is never actually used."""
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="u@x.com")
    app.dependency_overrides[get_google_auth] = lambda: {"access_token": "t", "refresh_token": None}
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield
    app.dependency_overrides.clear()


def _patches(project=PROJECT, matrix=(HEADERS, MATRIX_ROWS, False)):
    """Everything the handler reaches out to, replaced. Note `_filter_rows` is patched to
    the identity so its own tests stay the single source of truth for filtering."""
    return (
        patch("app.api.timeline._resolve_project", AsyncMock(return_value=project)),
        patch("app.api.timeline.build_sheets_service", MagicMock()),
        patch("app.api.timeline.get_tab_matrix", AsyncMock(return_value=matrix)),
        patch("app.api.timeline._resolver_for", AsyncMock(return_value=None)),
        patch("app.api.timeline._filter_rows",
              AsyncMock(side_effect=lambda db, pid, rows, *a, **k: rows)),
    )


def _get(url=ENDPOINT, **kwargs):
    ps = _patches(**kwargs)
    for p in ps:
        p.start()
    try:
        return client.get(url)
    finally:
        for p in ps:
            p.stop()


# --- the happy path ------------------------------------------------------------

def test_it_returns_groups_and_counts_for_a_tab():
    body = _get().json()
    assert body["counts"]["total"] == 3
    assert body["counts"]["charted"] == 1
    assert body["counts"]["milestone_only"] == 1
    assert body["counts"]["undated"] == 1
    assert {g["label"] for g in body["groups"]} == {"SD", "MM"}


def test_it_reports_the_project_and_tab_it_read():
    body = _get().json()
    assert body["tab"] == "SD"
    assert body["project_name"] == "HEDP Tracker"
    assert body["primary_id_column"] == "WRICEF No."


def test_it_names_the_columns_it_resolved():
    """"No bars" has two very different causes — no dates, or the wrong column — and the
    reader cannot tell them apart without this."""
    body = _get().json()
    assert body["start_header"] == "Start Date"
    assert body["due_header"] == "Expected Completetion Date"


def test_rows_carry_their_sheet_row_number():
    """api/digest.py omits data_start_row here; this route must not, or a bar cannot open
    an editable row on a tracker whose IDs repeat (§16.7)."""
    body = _get().json()
    numbers = [i["row_number"] for g in body["groups"] for i in g["items"]]
    assert 3 in numbers


def test_truncation_is_passed_through_rather_than_swallowed():
    body = _get(matrix=(HEADERS, MATRIX_ROWS, True)).json()
    assert body["truncated"] is True


# --- gating ---------------------------------------------------------------------

def test_a_project_the_caller_cannot_see_is_404_not_403():
    with patch("app.api.timeline._resolve_project",
               AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found"))):
        response = client.get(ENDPOINT)
    assert response.status_code == 404


# --- filtering ------------------------------------------------------------------

def test_the_grid_filters_are_forwarded_verbatim():
    """Reusing _filter_rows is what stops the chart and the grid disagreeing; this asserts
    the parameters actually arrive."""
    spy = AsyncMock(side_effect=lambda db, pid, rows, *a, **k: rows)
    with patch("app.api.timeline._resolve_project", AsyncMock(return_value=PROJECT)), \
         patch("app.api.timeline.build_sheets_service", MagicMock()), \
         patch("app.api.timeline.get_tab_matrix", AsyncMock(return_value=(HEADERS, MATRIX_ROWS, False))), \
         patch("app.api.timeline._resolver_for", AsyncMock(return_value=None)), \
         patch("app.api.timeline._filter_rows", spy):
        client.get(ENDPOINT + "?q=BOM&person=Sara%20Iqbal&overdue=true&role_key=dev&status=Open")

    kwargs = spy.await_args.kwargs
    assert kwargs["q"] == "BOM"
    assert kwargs["person"] == "Sara Iqbal"
    assert kwargs["overdue"] is True
    assert kwargs["role_key"] == "dev"
    assert kwargs["status"] == "Open"


def test_group_by_is_honoured():
    body = _get(url=ENDPOINT + "?group_by=Dev%20Status").json()
    assert body["group_by"] == "Dev Status"
    assert {g["label"] for g in body["groups"]} == {"In Progress", "Not Started", "Completed"}
