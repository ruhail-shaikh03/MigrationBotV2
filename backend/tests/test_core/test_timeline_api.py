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

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.aliases import PersonResolver
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
    # Only the three this fixture installed. `.clear()` would also discard overrides it
    # never set, so the first suite to adopt a module- or session-scoped override would
    # break here, order-dependently, with a failure that reads as an auth bug.
    for dependency in (get_current_user, get_google_auth, get_db):
        app.dependency_overrides.pop(dependency, None)


@contextmanager
def _stubs(project=PROJECT, matrix=(HEADERS, MATRIX_ROWS, False), resolver=None, filter_rows=None):
    """Everything the handler reaches out to, replaced, plus the spies worth asserting on.

    `_filter_rows` defaults to the identity so its own tests stay the single source of
    truth for filtering; `test_only_the_filtered_rows_are_charted` overrides it, because
    an identity stub cannot tell `filtered` from `rows`.

    Entered through an ExitStack so a raise part-way through unwinds the patches already
    applied. Started one at a time with bare `.start()` calls, a failure on the second
    would leak the first into every later test in the session.
    """
    read = AsyncMock(return_value=matrix)
    filtering = filter_rows or AsyncMock(side_effect=lambda db, pid, rows, *a, **k: rows)
    with ExitStack() as stack:
        for attribute, replacement in (
            ("_resolve_project", AsyncMock(return_value=project)),
            ("build_sheets_service", MagicMock()),
            ("get_tab_matrix", read),
            ("_resolver_for", AsyncMock(return_value=resolver)),
            ("_filter_rows", filtering),
        ):
            stack.enter_context(patch(f"app.api.timeline.{attribute}", replacement))
        yield SimpleNamespace(read=read, filter_rows=filtering)


def _get(url=ENDPOINT, **kwargs):
    with _stubs(**kwargs):
        return client.get(url)


# --- the happy path ------------------------------------------------------------

def test_it_returns_groups_and_counts_for_a_tab():
    body = _get().json()
    assert body["counts"]["total"] == 3
    assert body["counts"]["charted"] == 1
    assert body["counts"]["milestone_only"] == 1
    assert body["counts"]["undated"] == 1
    assert {g["label"] for g in body["groups"]} == {"SD", "MM"}


def test_it_reports_the_project_and_tab_it_read():
    with _stubs() as spies:
        body = client.get(ENDPOINT).json()

    assert body["tab"] == "SD"
    assert body["project_name"] == "HEDP Tracker"
    assert body["primary_id_column"] == "WRICEF No."
    # Task 7 renders the tab switcher from this key, so it has to be present even when a
    # flat single-tab config leaves it empty — there is nowhere to switch to.
    assert body["tabs"] == []
    # And that the tab it reports is the tab it actually read. The fixture's schema_config
    # is flat, so get_tab_schema answers the same dict whatever tab name it is handed, and
    # nothing else in the response would notice a handler labelling tab A while charting
    # tab B's rows. `data_start_row` is asserted here too: it reaches the read as well as
    # the _row_dicts call, and only the second of those has a test of its own.
    _service, spreadsheet_id, tab_read, data_start_row = spies.read.await_args.args
    assert (spreadsheet_id, tab_read, data_start_row) == ("sheet-123", "SD", 3)


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
    numbers = sorted(i["row_number"] for g in body["groups"] for i in g["items"])
    # The whole list, not a membership check: rows 3 and 4 are the two dated rows, and row
    # 5 is undated so it never becomes an item. `3 in numbers` would pass a handler that
    # numbered only some of them.
    assert numbers == [3, 4]


def test_truncation_is_passed_through_rather_than_swallowed():
    body = _get(matrix=(HEADERS, MATRIX_ROWS, True)).json()
    assert body["truncated"] is True


# --- gating ---------------------------------------------------------------------

def test_a_project_the_caller_cannot_see_is_404_not_403():
    """What this proves is propagation and ordering: the gate runs *first*, before any
    Sheets call — `get_tab_matrix` is deliberately left unpatched, so reaching it would
    error rather than 404 — and the handler does not swallow its refusal. The 404-rather-
    than-403 rule itself lives in `_resolve_project` and is tested with it."""
    with patch("app.api.timeline._resolve_project",
               AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found"))):
        response = client.get(ENDPOINT)
    assert response.status_code == 404


# --- tab resolution -------------------------------------------------------------

def test_a_project_with_no_default_tab_and_no_tab_argument_is_refused():
    """`_tab_for`'s refusal, which an inline `tab or project.default_tab` does not make.
    Charting whatever came back for an unnamed tab is worse than saying no: the caller
    would be reading a chart of a tab nobody chose."""
    tabless = SimpleNamespace(**{**vars(PROJECT), "default_tab": None})
    assert _get(project=tabless).status_code == 400


# --- filtering ------------------------------------------------------------------

def test_the_grid_filters_are_forwarded_verbatim():
    """Reusing _filter_rows is what stops the chart and the grid disagreeing; this asserts
    the parameters actually arrive."""
    with _stubs() as spies:
        client.get(ENDPOINT + "?q=BOM&person=Sara%20Iqbal&overdue=true&role_key=dev&status=Open")

    kwargs = spies.filter_rows.await_args.kwargs
    assert kwargs["q"] == "BOM"
    assert kwargs["person"] == "Sara Iqbal"
    assert kwargs["overdue"] is True
    assert kwargs["role_key"] == "dev"
    assert kwargs["status"] == "Open"


def test_only_the_filtered_rows_are_charted():
    """The spy above proves the filters *arrive*; this proves the filtered result is what
    gets drawn. Every other test stubs `_filter_rows` to the identity, which cannot tell a
    handler that charts `filtered` apart from one that charts the unfiltered `rows` — and
    the second silently ignores every filter the user set."""
    first_only = AsyncMock(side_effect=lambda db, pid, rows, *a, **k: rows[:1])
    with _stubs(filter_rows=first_only):
        body = client.get(ENDPOINT).json()

    assert body["counts"]["total"] == 1
    assert body["counts"]["charted"] == 1
    assert {g["label"] for g in body["groups"]} == {"SD"}


# --- aliasing --------------------------------------------------------------------

def test_the_alias_resolver_reaches_the_computation():
    """`_resolver_for` is stubbed to None everywhere else — and None is also
    `build_timeline`'s default for that parameter, so dropping `resolver=resolver` from the
    call changes nothing any other test can see. A resolver that actually merges a spelling
    makes it visible: the group takes the canonical name the project chose over the text in
    the cell, which is the whole point of the alias map (§7.6)."""
    resolver = PersonResolver.from_rows([SimpleNamespace(alias="Sara Iqbal", canonical="Sara I.")])
    with _stubs(resolver=resolver):
        body = client.get(ENDPOINT + "?group_by=Developer%20Name").json()

    labels = {g["label"] for g in body["groups"]}
    assert "Sara I." in labels
    assert "Sara Iqbal" not in labels


def test_group_by_is_honoured():
    body = _get(url=ENDPOINT + "?group_by=Dev%20Status").json()
    assert body["group_by"] == "Dev Status"
    assert {g["label"] for g in body["groups"]} == {"In Progress", "Not Started", "Completed"}
