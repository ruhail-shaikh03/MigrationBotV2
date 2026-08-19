"""The durable Postgres tier, and the three-tier read it turns `get_tab_matrix` into.

The Redis tier answers freshness with a timer: sixty seconds after a scan it assumes the
data is stale whether or not anything changed. This tier answers with a fact, so an
unchanged tab costs nothing to read again however long it stays unchanged. These tests pin
down the two things that makes true, and the one thing that would make it dangerous:

* the tier ordering, asserted by call counts - a clean tab must reach neither Sheets nor
  Drive, because "no API calls" is the entire claim;
* the degradation, asserted by behaviour - every failure here has to become a live scan,
  never an error, since a cache is an optimisation and the spreadsheet is always still there;
* the refill's row construction, asserted directly - ragged rows, blank IDs, and above all
  duplicated IDs, which must survive rather than collapse (§16.7).

Functions under test are imported by name at module scope on purpose: `conftest.py`'s
autouse `cold_record_cache` fixture patches these same names on the *module*, and binding
them here before that patch applies is what lets these tests exercise the real
implementations while every other test in the suite keeps getting the inert ones.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.sheets.drive import WATCH_RENEW_MARGIN_SECONDS
from app.sheets.records_cache import (
    FRESHNESS_GRACE_SECONDS,
    _build_records,
    _clean_without_drive,
    _maybe_renew_channel,
    mark_dirty,
    read_tab,
    store_tab,
)
from app.sheets.rows_cache import get_tab_matrix

HEADERS = ["Module", "WRICEF No.", "Dev Status", "Developer Name"]
DATA = [
    ["SD", "W-1", "Completed", "Sara Iqbal"],
    ["MM", "W-2", "In Progress", "Bilal Ahmed"],
    ["SD", "W-3", "In Progress", "Sara Iqbal"],
]
NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def _state(checked_at=None):
    return SimpleNamespace(checked_at=checked_at)


def _channel(expires_in_seconds):
    return SimpleNamespace(expiration=NOW + timedelta(seconds=expires_in_seconds))


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


def _cold_redis():
    client = AsyncMock()
    client.get.return_value = None
    return client


# --- when freshness is free --------------------------------------------------

def test_a_live_push_channel_makes_the_tab_clean_with_no_api_call():
    """The steady state. Drive has undertaken to tell us, so silence is informative."""
    assert _clean_without_drive(_state(), _channel(expires_in_seconds=3600), NOW) is True


def test_an_expired_channel_is_not_evidence_of_anything():
    """A lapsed channel stops notifying silently — trusting it would serve stale rows
    indefinitely, which is the one failure this layer must not have."""
    assert _clean_without_drive(_state(), _channel(expires_in_seconds=-1), NOW) is False


def test_a_recent_confirmation_is_trusted_inside_the_grace_window():
    recent = NOW - timedelta(seconds=FRESHNESS_GRACE_SECONDS - 5)
    assert _clean_without_drive(_state(checked_at=recent), None, NOW) is True


def test_an_old_confirmation_is_not():
    stale = NOW - timedelta(seconds=FRESHNESS_GRACE_SECONDS + 5)
    assert _clean_without_drive(_state(checked_at=stale), None, NOW) is False


def test_a_never_confirmed_tab_with_no_channel_must_be_checked():
    assert _clean_without_drive(_state(checked_at=None), None, NOW) is False


# --- what a refill stores ----------------------------------------------------

def test_duplicated_primary_ids_both_survive_a_refill():
    """§16.7: 27 of 412 rows on the reference tracker share a WRICEF No. A unique index
    would have silently dropped the second of each, turning a known reporting oddity into
    invisible data loss."""
    rows = [["SD", "W-1", "Completed", "A"], ["MM", "W-1", "Draft", "B"]]
    records = _build_records("sheet-1", "SD", 3, rows, primary_idx=1, now=NOW)

    assert len(records) == 2
    assert [r["primary_id"] for r in records] == ["W-1", "W-1"]
    # Distinct rows, distinguished by position rather than by identity.
    assert [r["row_number"] for r in records] == [3, 4]
    assert [r["data"][3] for r in records] == ["A", "B"]


def test_row_numbers_are_offsets_from_the_start_row_the_scan_used():
    records = _build_records("sheet-1", "SD", 7, DATA, primary_idx=1, now=NOW)
    assert [r["row_number"] for r in records] == [7, 8, 9]


def test_ids_are_normalised_the_way_find_row_num_compares_them():
    rows = [["SD", "  w-1 ", "", ""]]
    records = _build_records("sheet-1", "SD", 3, rows, primary_idx=1, now=NOW)
    assert records[0]["primary_id"] == "W-1"


def test_a_blank_id_becomes_null_rather_than_an_empty_string():
    """Two blanks must not compare equal to each other as if they named the same row."""
    rows = [["SD", "   ", "", ""]]
    records = _build_records("sheet-1", "SD", 3, rows, primary_idx=1, now=NOW)
    assert records[0]["primary_id"] is None


def test_a_row_shorter_than_the_id_column_does_not_raise():
    """Sheets omits trailing empty cells, so a sparse row can end before the ID column."""
    rows = [["SD"]]
    records = _build_records("sheet-1", "SD", 3, rows, primary_idx=1, now=NOW)
    assert records[0]["primary_id"] is None
    assert records[0]["data"] == ["SD"]


def test_rows_are_stored_exactly_as_scanned_not_padded():
    """Callers pad and index positionally themselves; storing the scan verbatim is what
    keeps a cached read byte-identical to a live one."""
    rows = [["SD", "W-1"]]
    records = _build_records("sheet-1", "SD", 3, rows, primary_idx=1, now=NOW)
    assert records[0]["data"] == ["SD", "W-1"]


# --- tier ordering, which is the whole point ---------------------------------

@pytest.mark.asyncio
async def test_a_clean_tab_is_served_from_postgres_with_no_sheets_and_no_drive_calls():
    """The headline claim. Previously this cost a full paginated scan every 60 seconds,
    forever, on a tab nobody had touched in a month."""
    service, calls = _counting_service()
    drive = AsyncMock(return_value="2026-08-19T00:00:00.000Z")

    with patch("app.sheets.rows_cache.redis_client", _cold_redis()), \
         patch("app.sheets.records_cache.read_tab",
               AsyncMock(return_value=(HEADERS, DATA, False))), \
         patch("app.sheets.drive.get_modified_time", drive):
        headers, rows, truncated = await get_tab_matrix(service, "sheet-1", "SD", 3)

    assert headers == HEADERS
    assert rows == DATA
    assert truncated is False
    assert calls == [], "a clean tab must not touch the Sheets API at all"
    assert drive.await_count == 0, "nor spend a Drive call to re-establish what it knows"


@pytest.mark.asyncio
async def test_a_dirty_tab_scans_live_and_stores_the_result():
    service, calls = _counting_service()
    store = AsyncMock()

    with patch("app.sheets.rows_cache.redis_client", _cold_redis()), \
         patch("app.sheets.records_cache.read_tab", AsyncMock(return_value=None)), \
         patch("app.sheets.records_cache.store_tab", store), \
         patch("app.sheets.drive.get_modified_time",
               AsyncMock(return_value="2026-08-19T00:00:00.000Z")):
        headers, rows, _ = await get_tab_matrix(service, "sheet-1", "SD", 3)

    assert rows == DATA
    assert calls, "a dirty tab must actually re-read the sheet"
    store.assert_awaited_once()
    assert store.await_args.kwargs["drive_modified_time"] == "2026-08-19T00:00:00.000Z"
    assert store.await_args.kwargs["data_start_row"] == 3


@pytest.mark.asyncio
async def test_the_version_stamp_is_read_before_the_scan_not_after():
    """An edit landing mid-scan must show up as a mismatch next time. Stamping afterwards
    would record that edit as already captured and serve rows that never contained it."""
    service, calls = _counting_service()
    order = []

    async def _modified(*_args, **_kwargs):
        order.append("drive")
        return "2026-08-19T00:00:00.000Z"

    def _get(spreadsheetId, range):
        order.append("sheets")
        execution = MagicMock()
        start, end = range.split("!")[1].split(":")
        execution.execute.return_value = (
            {"values": [HEADERS]} if start == end else {"values": DATA}
        )
        return execution

    service.spreadsheets.return_value.values.return_value.get.side_effect = _get

    with patch("app.sheets.rows_cache.redis_client", _cold_redis()), \
         patch("app.sheets.records_cache.read_tab", AsyncMock(return_value=None)), \
         patch("app.sheets.records_cache.store_tab", AsyncMock()), \
         patch("app.sheets.drive.get_modified_time", _modified):
        await get_tab_matrix(service, "sheet-1", "SD", 3)

    assert order[0] == "drive", f"expected the version stamp first, got {order}"
    assert "sheets" in order


@pytest.mark.asyncio
async def test_a_warm_redis_key_short_circuits_postgres_entirely():
    """Tier 1 exists to keep a burst of grid clicks off tier 2, so it must answer alone."""
    import json

    client = AsyncMock()
    client.get.return_value = json.dumps(
        {"headers": HEADERS, "rows": DATA, "truncated": False}
    )
    durable = AsyncMock(return_value=None)
    service, calls = _counting_service()

    with patch("app.sheets.rows_cache.redis_client", client), \
         patch("app.sheets.records_cache.read_tab", durable):
        headers, rows, _ = await get_tab_matrix(service, "sheet-1", "SD", 3)

    assert rows == DATA
    assert durable.await_count == 0
    assert calls == []


# --- keeping the channel alive without a scheduler ---------------------------

def _live_channel(expires_in_seconds):
    return SimpleNamespace(
        spreadsheet_id="sheet-1",
        channel_id="chan-old",
        resource_id="res-old",
        token="tok",
        expiration=NOW + timedelta(seconds=expires_in_seconds),
    )


def _renewal_patches(register, stop):
    return (
        patch.object(settings, "PUBLIC_BASE_URL", "https://example.test"),
        patch.object(settings, "DRIVE_WEBHOOK_TOKEN", "tok"),
        patch("app.sheets.client.drive_service_for", MagicMock(return_value=MagicMock())),
        patch("app.sheets.drive.register_watch", register),
        patch("app.sheets.drive.stop_watch", stop),
    )


@pytest.mark.asyncio
async def test_a_channel_far_from_expiry_is_left_alone():
    """Renewal rides on ordinary traffic, so it must not fire on ordinary traffic."""
    register, stop = AsyncMock(), AsyncMock()
    patches = _renewal_patches(register, stop)
    for p in patches:
        p.start()
    try:
        channel = _live_channel(WATCH_RENEW_MARGIN_SECONDS + 3600)
        await _maybe_renew_channel(AsyncMock(), MagicMock(), channel, NOW)
    finally:
        for p in patches:
            p.stop()

    assert register.await_count == 0
    assert channel.channel_id == "chan-old"


@pytest.mark.asyncio
async def test_a_channel_inside_the_margin_is_renewed_and_the_row_repointed():
    """Without this the push mechanism decays into a modifiedTime poll after a day, and
    nothing breaks loudly enough for anyone to notice."""
    register = AsyncMock(return_value={
        "channel_id": "chan-new",
        "resource_id": "res-new",
        "token": "tok",
        "expiration": NOW + timedelta(days=1),
    })
    stop = AsyncMock()
    patches = _renewal_patches(register, stop)
    for p in patches:
        p.start()
    try:
        channel = _live_channel(60)
        await _maybe_renew_channel(AsyncMock(), MagicMock(), channel, NOW)
    finally:
        for p in patches:
            p.stop()

    register.assert_awaited_once()
    assert channel.channel_id == "chan-new"
    assert channel.resource_id == "res-new"
    # The old channel is retired only after the new one exists, so there is never a window
    # with no channel watching the file.
    stop.assert_awaited_once_with(stop.await_args.args[0], "chan-old", "res-old")


@pytest.mark.asyncio
async def test_a_failed_renewal_leaves_the_existing_channel_untouched():
    """Renewal is opportunistic. Failing it must not also destroy the channel we still have."""
    register, stop = AsyncMock(return_value=None), AsyncMock()
    patches = _renewal_patches(register, stop)
    for p in patches:
        p.start()
    try:
        channel = _live_channel(60)
        await _maybe_renew_channel(AsyncMock(), MagicMock(), channel, NOW)
    finally:
        for p in patches:
            p.stop()

    assert channel.channel_id == "chan-old"
    assert stop.await_count == 0


@pytest.mark.asyncio
async def test_renewal_does_nothing_when_push_is_not_configured():
    register, stop = AsyncMock(), AsyncMock()
    with patch.object(settings, "PUBLIC_BASE_URL", ""), \
         patch.object(settings, "DRIVE_WEBHOOK_TOKEN", ""), \
         patch("app.sheets.drive.register_watch", register):
        await _maybe_renew_channel(AsyncMock(), MagicMock(), _live_channel(60), NOW)

    assert register.await_count == 0


# --- degradation -------------------------------------------------------------

@pytest.mark.asyncio
async def test_postgres_being_unreachable_degrades_to_a_live_scan_not_an_error():
    """Same rule Redis already lives under: losing a cache costs quota, never correctness."""
    service, calls = _counting_service()

    with patch("app.sheets.rows_cache.redis_client", _cold_redis()), \
         patch("app.sheets.records_cache.AsyncSessionLocal",
               MagicMock(side_effect=ConnectionError("postgres is down"))), \
         patch("app.sheets.drive.get_modified_time", AsyncMock(return_value=None)):
        headers, rows, _ = await get_tab_matrix(service, "sheet-1", "SD", 3)

    assert rows == DATA, "the read must still return the right answer"
    assert calls, "it must fall back to reading the sheet"


@pytest.mark.asyncio
async def test_read_tab_returns_none_when_the_database_is_unreachable():
    with patch("app.sheets.records_cache.AsyncSessionLocal",
               MagicMock(side_effect=ConnectionError("postgres is down"))):
        assert await read_tab(MagicMock(), "sheet-1", "SD", 3) is None


@pytest.mark.asyncio
async def test_store_tab_swallows_a_database_failure():
    """A cache that could not be written is a slower next read, never a failed current one."""
    with patch("app.sheets.records_cache.AsyncSessionLocal",
               MagicMock(side_effect=ConnectionError("postgres is down"))):
        await store_tab("sheet-1", "SD", 3, HEADERS, DATA, False, None)


@pytest.mark.asyncio
async def test_mark_dirty_swallows_a_database_failure():
    """Invalidation runs after a successful write. It must never turn one into a failure."""
    with patch("app.sheets.records_cache.AsyncSessionLocal",
               MagicMock(side_effect=ConnectionError("postgres is down"))):
        await mark_dirty("sheet-1", "SD")


@pytest.mark.asyncio
async def test_invalidating_a_tab_clears_both_tiers():
    """Dropping only the Redis key would leave Postgres claiming the tab is clean, and the
    next read would be served pre-write rows with no TTL to eventually rescue them."""
    from app.sheets.rows_cache import invalidate_tab

    client = AsyncMock()
    dirty = AsyncMock()

    with patch("app.sheets.rows_cache.redis_client", client), \
         patch("app.sheets.records_cache.mark_dirty", dirty):
        await invalidate_tab("sheet-1", "SD")

    client.delete.assert_awaited_once()
    dirty.assert_awaited_once_with("sheet-1", "SD")
