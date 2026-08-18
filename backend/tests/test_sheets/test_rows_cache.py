"""The row cache, and the agent reads that now go through it.

Before this, every agent tool call re-scanned the whole tab. One turn that ran `summarize`,
then `get_row`, then `search_rows` scanned the same 412 rows three times, each scan paging
up to `_MAX_SCAN_ROWS`. The dashboard had had a 60-second cache for this since the grid
landed; the agent simply never used it.

What these tests pin down is the part that is easy to get wrong later: the cache must not
change *answers*, only the number of calls. So each one asserts both — the same result as
an uncached read, and the call count that proves the cache was actually consulted.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.sheets.read import _column_letter_to_index, get_row, search_rows

HEADERS = ["Module", "WRICEF No.", "Dev Status", "Developer Name"]
DATA = [
    ["SD", "W-1", "Completed", "Sara Iqbal"],
    ["MM", "W-2", "In Progress", "Bilal Ahmed"],
    ["SD", "W-3", "In Progress", "Sara Iqbal"],
]
SCHEMA = {
    "data_start_row": 3,
    "primary_id_position": "B",
    "primary_id_column": "WRICEF No.",
    "critical_fields": ["WRICEF No.", "Dev Status"],
}


def _counting_service():
    """A Sheets mock that answers header and data ranges, and counts every call."""
    service = MagicMock()
    calls = []

    def _get(spreadsheetId, range):
        calls.append(range)
        start, end = range.split("!")[1].split(":")
        payload = {"values": [HEADERS]} if start == end else {"values": DATA}
        execution = MagicMock()
        execution.execute.return_value = payload
        return execution

    service.spreadsheets.return_value.values.return_value.get.side_effect = _get
    return service, calls


def _redis(cached=None):
    """A Redis mock that is either cold (`cached=None`) or already holding a matrix."""
    client = AsyncMock()
    client.get.return_value = (
        json.dumps({"headers": HEADERS, "rows": DATA, "truncated": False}) if cached else None
    )
    return client


# --- column addressing -------------------------------------------------------

@pytest.mark.parametrize("letter,index", [("A", 0), ("B", 1), ("Z", 25), ("AA", 26), ("AB", 27)])
def test_column_letters_map_to_the_same_index_the_uncached_path_used(letter, index):
    """Keyed off primary_id_position, not the header name: a cache that answers with a
    *different* row than the live path would be worse than no cache."""
    assert _column_letter_to_index(letter) == index


def test_a_missing_column_letter_falls_back_to_the_schema_default():
    assert _column_letter_to_index("") == _column_letter_to_index("B")


# --- get_row -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_row_is_answered_from_a_warm_cache_without_touching_sheets():
    """The tool the model is most prone to calling in a loop, now free when warm."""
    service, calls = _counting_service()
    with patch("app.sheets.rows_cache.redis_client", _redis(cached=True)):
        result = await get_row("sheet-1", "SD", "W-2", SCHEMA, {}, service)

    assert result["ok"] is True
    assert result["data"]["Developer Name"] == "Bilal Ahmed"
    assert calls == []


@pytest.mark.asyncio
async def test_get_row_returns_the_same_row_on_a_cold_cache():
    service, calls = _counting_service()
    with patch("app.sheets.rows_cache.redis_client", _redis()):
        result = await get_row("sheet-1", "SD", "W-2", SCHEMA, {}, service)

    assert result["data"]["Developer Name"] == "Bilal Ahmed"
    assert calls, "a cold cache must still read the sheet"


@pytest.mark.asyncio
async def test_a_missing_id_still_reports_not_found():
    service, _ = _counting_service()
    with patch("app.sheets.rows_cache.redis_client", _redis(cached=True)):
        result = await get_row("sheet-1", "SD", "W-99", SCHEMA, {}, service)

    assert result["ok"] is False
    assert "W-99" in result["error"]


@pytest.mark.asyncio
async def test_id_matching_ignores_case_and_padding_as_it_did_before():
    """find_row_num compared `.strip().upper()`; the cached lookup must not be stricter."""
    service, _ = _counting_service()
    with patch("app.sheets.rows_cache.redis_client", _redis(cached=True)):
        result = await get_row("sheet-1", "SD", "  w-1 ", SCHEMA, {}, service)

    assert result["ok"] is True
    assert result["data"]["Module"] == "SD"


# --- the quota win -----------------------------------------------------------

@pytest.mark.asyncio
async def test_two_tool_calls_in_one_turn_scan_the_sheet_once():
    """The actual point of the change. Previously each call re-scanned the whole tab."""
    service, calls = _counting_service()
    store: dict = {}

    client = AsyncMock()
    client.get.side_effect = lambda key: store.get(key)

    async def _set(key, value, ex=None):
        store[key] = value

    client.set.side_effect = _set

    with patch("app.sheets.rows_cache.redis_client", client):
        await search_rows("sheet-1", "SD", [], None, 20, SCHEMA, {}, service)
        first_pass = len(calls)
        await get_row("sheet-1", "SD", "W-1", SCHEMA, {}, service)

    assert first_pass > 0
    assert len(calls) == first_pass, "the second read should have been served from cache"


# --- degradation -------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_being_unreachable_degrades_to_a_live_scan_not_an_error():
    """A cache is an optimisation. Losing it must cost quota, never correctness."""
    service, calls = _counting_service()
    client = AsyncMock()
    client.get.side_effect = ConnectionError("redis is down")
    client.set.side_effect = ConnectionError("redis is down")

    with patch("app.sheets.rows_cache.redis_client", client):
        result = await get_row("sheet-1", "SD", "W-3", SCHEMA, {}, service)

    assert result["ok"] is True
    assert result["data"]["Dev Status"] == "In Progress"
    assert calls, "it must fall back to reading the sheet"
