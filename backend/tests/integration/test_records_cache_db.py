"""The Postgres row cache against a real database.

These exist because the unit tests structurally cannot catch the failure that matters most
here. `test_records_cache.py` mocks `store_tab` in order to assert the tier ordering, so a
`store_tab` that silently writes nothing would leave every one of those tests green while
the entire cache did nothing at all - reads would miss forever and simply fall back to the
Sheets scan they always did, which is the *old* behaviour and therefore invisible.

That is not hypothetical. The first version of `store_tab` called `_primary_id_index`
(a SELECT) before opening `async with db.begin()`. A SQLAlchemy 2.0 session autobegins on
its first statement, so `begin()` then raised InvalidRequestError, which the function's
blanket `except Exception` turned into a log line. Every unit test passed.

Only a real round-trip catches that class of bug, so these tests do exactly one thing: write
through the real code path, read back through the real code path, and check the rows.

Requires Postgres, and so lives in `tests/integration/` alongside the other tests that do.
CI provides one as a service container and runs the whole suite; locally the directory is
excluded (`pytest tests/test_core tests/test_sheets`), which is the same arrangement
`test_integration_logic.py` already relies on.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from app.db.engine import AsyncSessionLocal, drop_db, init_db
from app.models.sheet_record import SheetRecord
from app.sheets.records_cache import mark_dirty, read_tab, store_tab
from tests.conftest import require_test_database

pytestmark = pytest.mark.asyncio

SPREADSHEET = "sheet-records-it"
TAB = "SD"
START = 3
HEADERS = ["Module", "WRICEF No.", "Dev Status", "Developer Name"]
DATA = [
    ["SD", "W-1", "Completed", "Sara Iqbal"],
    ["MM", "W-2", "In Progress", "Bilal Ahmed"],
    ["SD", "W-3", "In Progress", "Sara Iqbal"],
]


@pytest.fixture(autouse=True)
async def setup_records_db():
    require_test_database()
    try:
        await drop_db()
    except Exception:
        pass
    await init_db()
    yield
    await drop_db()


def _service():
    """A Sheets service stand-in carrying no credentials.

    `client.drive_service_for` returns None for it, so `get_modified_time` returns None and
    no Drive call is attempted - which is what we want, since these tests are about the SQL,
    not about change detection.
    """
    return MagicMock()


async def _row_count() -> int:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(SheetRecord)
                .where(SheetRecord.spreadsheet_id == SPREADSHEET, SheetRecord.tab == TAB)
            )
        ).scalar_one()


async def test_a_stored_tab_reads_back_identically():
    """The whole contract in one test: what goes in comes out, byte for byte.

    Freshness is satisfied by the grace window here - `store_tab` sets `checked_at`, so the
    immediately following read is clean without needing a watch channel or a Drive call.
    """
    await store_tab(SPREADSHEET, TAB, START, HEADERS, DATA, False, "2026-08-19T00:00:00.000Z")

    result = await read_tab(_service(), SPREADSHEET, TAB, START)

    assert result is not None, "a freshly stored tab must read back as clean"
    headers, rows, truncated = result
    assert headers == HEADERS
    assert rows == DATA
    assert truncated is False


async def test_rows_come_back_in_sheet_order():
    """Callers index positionally and treat this as the sheet. Order is part of the answer."""
    await store_tab(SPREADSHEET, TAB, START, HEADERS, DATA, False, None)

    _, rows, _ = await read_tab(_service(), SPREADSHEET, TAB, START)

    assert [r[1] for r in rows] == ["W-1", "W-2", "W-3"]


async def test_both_rows_of_a_duplicated_id_survive_the_round_trip():
    """§16.7: 27 of 412 rows on the reference tracker share a WRICEF No. A unique index on
    primary_id would have dropped one of each here, silently."""
    duplicated = [
        ["SD", "W-1", "Completed", "Sara Iqbal"],
        ["MM", "W-1", "Draft", "Bilal Ahmed"],
    ]
    await store_tab(SPREADSHEET, TAB, START, HEADERS, duplicated, False, None)

    _, rows, _ = await read_tab(_service(), SPREADSHEET, TAB, START)

    assert len(rows) == 2
    assert [r[3] for r in rows] == ["Sara Iqbal", "Bilal Ahmed"]
    assert await _row_count() == 2


async def test_a_refill_replaces_the_tab_rather_than_appending_to_it():
    """Rows shift when someone inserts above them, so a refill cannot be an upsert."""
    await store_tab(SPREADSHEET, TAB, START, HEADERS, DATA, False, None)
    assert await _row_count() == 3

    shorter = [["SD", "W-9", "Draft", "Nobody"]]
    await store_tab(SPREADSHEET, TAB, START, HEADERS, shorter, False, None)

    assert await _row_count() == 1
    _, rows, _ = await read_tab(_service(), SPREADSHEET, TAB, START)
    assert rows == shorter


async def test_marking_dirty_stops_the_tab_being_served():
    """What the worker and the webhook both rely on."""
    await store_tab(SPREADSHEET, TAB, START, HEADERS, DATA, False, None)
    assert await read_tab(_service(), SPREADSHEET, TAB, START) is not None

    await mark_dirty(SPREADSHEET, TAB)

    assert await read_tab(_service(), SPREADSHEET, TAB, START) is None
    # The rows are kept, not deleted — a refill replaces them.
    assert await _row_count() == 3


async def test_marking_a_whole_spreadsheet_dirty_covers_every_tab():
    """Drive notifications are file-level and cannot name a tab."""
    await store_tab(SPREADSHEET, "SD", START, HEADERS, DATA, False, None)
    await store_tab(SPREADSHEET, "MM", START, HEADERS, DATA, False, None)

    await mark_dirty(SPREADSHEET)

    assert await read_tab(_service(), SPREADSHEET, "SD", START) is None
    assert await read_tab(_service(), SPREADSHEET, "MM", START) is None


async def test_a_different_start_row_is_a_miss_not_a_wrong_answer():
    """Every cached row_number is an offset from the start row it was scanned at, so a
    caller using a different one is asking about differently-shaped data."""
    await store_tab(SPREADSHEET, TAB, START, HEADERS, DATA, False, None)

    assert await read_tab(_service(), SPREADSHEET, TAB, START + 1) is None


async def test_an_unknown_tab_is_a_miss():
    assert await read_tab(_service(), SPREADSHEET, "NeverScanned", START) is None


async def test_a_ragged_row_survives_storage_unpadded():
    """Sheets omits trailing empty cells. Padding here would change what callers compute."""
    ragged = [["SD", "W-1"], ["MM", "W-2", "In Progress", "Bilal Ahmed"]]
    await store_tab(SPREADSHEET, TAB, START, HEADERS, ragged, False, None)

    _, rows, _ = await read_tab(_service(), SPREADSHEET, TAB, START)

    assert rows == ragged


async def test_an_empty_tab_round_trips_as_empty_rather_than_as_a_miss():
    """A tracker with a header row and no data is a legitimate state, and re-scanning it on
    every read because 'no rows' looked like 'no cache' would defeat the point."""
    await store_tab(SPREADSHEET, TAB, START, HEADERS, [], False, None)

    result = await read_tab(_service(), SPREADSHEET, TAB, START)

    assert result is not None
    headers, rows, _ = result
    assert headers == HEADERS
    assert rows == []
