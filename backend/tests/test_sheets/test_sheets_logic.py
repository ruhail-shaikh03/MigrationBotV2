import pytest
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from googleapiclient.errors import HttpError

from app.sheets.retry import _with_retry
from app.sheets.read import find_row_num, get_bulk_rows_raw, search_rows, summarize, run_data_quality_check
from app.queue.producer import enqueue_write_job
from app.queue.worker import start_worker
from app.queue.events import publish_queue_update, queue_events_channel

# 1. Test transient rate-limiting retry backoff
@pytest.mark.asyncio
async def test_api_retry_backoff():
    """Verify that Google Sheets HTTP 429 rate limit triggers exponential backoff retries."""
    call_count = 0
    
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.reason = "Too Many Requests"
    
    def mock_api_call():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise HttpError(resp=mock_resp, content=b"Rate Limit Exceeded")
        return "success"

    # Call retry wrapper with small base delay for fast tests
    res = await _with_retry(mock_api_call, max_attempts=4, base_delay=0.01)
    
    assert res == "success"
    assert call_count == 3


# 2. Test row mapping locator
@pytest.mark.asyncio
async def test_read_find_row():
    """Verify that RICEFW ID index matches the target spreadsheet row index."""
    mock_service = MagicMock()
    
    mock_get = MagicMock()
    mock_get.execute.return_value = {
        "values": [
            ["SD-001"],
            ["SD-002"],
            ["SD-003"]
        ]
    }
    
    mock_service.spreadsheets().values().get.return_value = mock_get
    
    row_num = await find_row_num(
        service=mock_service,
        spreadsheet_id="sheet-123",
        sheet_name="SD",
        ricefw_id="SD-002",
        data_start_row=3,
        primary_id_pos="B"
    )
    
    assert row_num == 4  # Index 1 + data_start_row 3 = row 4


# 3. Test queue job enqueuing
@pytest.mark.asyncio
async def test_write_job_enqueuing():
    """Verify write tools properly format and LPUSH JSON jobs to Redis."""
    mock_redis = AsyncMock()
    
    with patch("app.queue.producer.redis_client", mock_redis):
        job = await enqueue_write_job(
            user_email="test@example.com",
            google_access_token="mock-token",
            session_id=None,
            tool_name="update_cell",
            spreadsheet_id="sheet-123",
            sheet_tab="SD",
            args={"ricefw_id": "SD-002", "updates": [{"field": "Dev Status", "value": "Done"}]},
            old_values={"Dev Status": "In Progress"}
        )
        
        assert job.id is not None
        assert mock_redis.rpush.called
        
        # Verify arguments passed to Redis rpush
        args, kwargs = mock_redis.rpush.call_args
        queue_key, raw_payload = args
        assert queue_key == "migrationbot:write_queue"
        
        payload_dict = json.loads(raw_payload)
        assert payload_dict["job_id"] == job.id
        assert payload_dict["payload"]["user_email"] == "test@example.com"
        assert payload_dict["payload"]["google_access_token"] == "mock-token"


# 4. Test queue worker rate-limiting throttling
@pytest.mark.asyncio
async def test_worker_throttling():
    """Verify that background worker enforces a rate limit sleep between queue events."""
    mock_redis = AsyncMock()
    
    call_count = 0
    job_envelope = {
        "job_id": "job-123",
        "payload": {
            "user_email": "test@example.com",
            "google_access_token": "token-123",
            "session_id": None,
            "tool_name": "format_row",
            "spreadsheet_id": "sheet-123",
            "sheet_tab": "SD",
            "args": {"ricefw_id": "SD-001", "color": "green"},
            "old_values": {}
        }
    }
    
    async def mock_blpop(key, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (key, json.dumps(job_envelope))
        else:
            raise asyncio.CancelledError("Stop loop")
            
    mock_redis.blpop.side_effect = mock_blpop
    
    # Patch process_job and asyncio.sleep to run instantly but track calls
    with patch("app.queue.worker.process_job", AsyncMock()) as mock_process, \
         patch("app.queue.worker.aioredis.from_url", return_value=mock_redis), \
         patch("asyncio.sleep", AsyncMock()) as mock_sleep:
         
        try:
            await start_worker()
        except asyncio.CancelledError:
            pass
            
        # Verify that sleep(1.0) was executed after job run
        mock_sleep.assert_called_with(1.0)
        assert mock_process.called


# 5. Test queue_update event payload shape published back to the client
@pytest.mark.asyncio
async def test_queue_update_event_payload():
    """Verify queue_update events carry the fields the frontend toast handler reads."""
    mock_redis = AsyncMock()

    with patch("app.queue.events.redis_client", mock_redis):
        event = await publish_queue_update(
            user_email="Test@Example.com",
            job_id="job-abc",
            status="completed",
            tool_name="update_cell",
            args={"ricefw_id": "SD-002"},
            session_id=None,
            error=None
        )

    assert mock_redis.publish.called
    channel, raw = mock_redis.publish.call_args[0]

    # Channel is namespaced per user (lowercased) so events only reach that user's sockets
    assert channel == queue_events_channel("test@example.com")

    published = json.loads(raw)
    assert published == event
    # chat/page.tsx reads: data.type, data.job_id, data.status, data.tool_name, data.args.ricefw_id
    assert published["type"] == "queue_update"
    assert published["job_id"] == "job-abc"
    assert published["status"] == "completed"
    assert published["tool_name"] == "update_cell"
    assert published["args"]["ricefw_id"] == "SD-002"


# 6. Test publishing failures never break the write job
@pytest.mark.asyncio
async def test_queue_update_survives_redis_outage():
    """Verify a Redis publish failure is swallowed so a completed write still finalizes."""
    mock_redis = AsyncMock()
    mock_redis.publish.side_effect = ConnectionError("Redis unreachable")

    with patch("app.queue.events.redis_client", mock_redis):
        event = await publish_queue_update(
            user_email="test@example.com",
            job_id="job-def",
            status="failed",
            tool_name="add_row",
            args={},
            error="Sheet not found"
        )

    assert event["status"] == "failed"
    assert event["error"] == "Sheet not found"


# 7. Test bulk pre-read resolves filter columns with the live column map
@pytest.mark.asyncio
async def test_bulk_rows_raw_uses_real_column_map():
    """Verify get_bulk_rows_raw resolves filter_by aliases instead of passing an empty map."""
    mock_service = MagicMock()
    column_map = {"Dev Status": ["dev status", "status", "progress"]}

    with patch("app.sheets.read.search_rows", AsyncMock(return_value={"rows": []})) as mock_search, \
         patch("app.sheets.read.get_row_raw", AsyncMock(return_value={})):

        await get_bulk_rows_raw(
            spreadsheet_id="sheet-123",
            active_tab="SD",
            args={"filter_by": {"field": "status", "value": "In Progress"}, "set_field": "Dev Status"},
            schema_config={"data_start_row": 3, "primary_id_position": "B"},
            service=mock_service,
            column_map=column_map
        )

    assert mock_search.called
    assert mock_search.call_args.kwargs["column_map"] == column_map


# 8. Test bulk pre-read falls back to the schema's column map when none is passed
@pytest.mark.asyncio
async def test_bulk_rows_raw_falls_back_to_schema_column_map():
    """Verify the column map is sourced from schema_config when the caller omits it."""
    mock_service = MagicMock()
    schema_column_map = {"Dev Status": ["dev status", "status"]}

    with patch("app.sheets.read.search_rows", AsyncMock(return_value={"rows": []})) as mock_search, \
         patch("app.sheets.read.get_row_raw", AsyncMock(return_value={})):

        await get_bulk_rows_raw(
            spreadsheet_id="sheet-123",
            active_tab="SD",
            args={"filter_by": {"field": "status", "value": "In Progress"}, "set_field": "Dev Status"},
            schema_config={
                "tabs": {"SD": {"data_start_row": 3, "column_map": schema_column_map}}
            },
            service=mock_service
        )

    assert mock_search.call_args.kwargs["column_map"] == schema_column_map


def _mock_service_for_sheet(headers_row, data_rows):
    """Build a mock Sheets service whose values().get() replies differently for the
    single-row header fetch vs. the bulk data-row fetch, keyed off the requested range."""
    mock_service = MagicMock()

    def mock_get(spreadsheetId, range):
        mock_exec = MagicMock()
        start, end = range.split("!")[1].split(":")
        if start == end:
            mock_exec.execute.return_value = {"values": [headers_row]}
        else:
            mock_exec.execute.return_value = {"values": data_rows}
        return mock_exec

    mock_service.spreadsheets.return_value.values.return_value.get.side_effect = mock_get
    return mock_service


# 9. Regression for TDD §16.2: search_rows must resolve columns whose real sheet header
# has a trailing space, since get_header_row strips headers but resolve_column's canonical
# names (from COLUMN_ALIASES) deliberately keep the sheet's real trailing space.
@pytest.mark.asyncio
async def test_search_rows_resolves_trailing_space_column():
    """Verify a filter on a natural-language term ('developer') matches a header stored
    with a trailing space ('Technical Resource ')."""
    mock_service = _mock_service_for_sheet(
        headers_row=["RICEFW ID", "Module", "Technical Resource "],
        data_rows=[
            ["SD-001", "SD", "Ruhail"],
            ["SD-002", "SD", "Alex"],
        ]
    )

    result = await search_rows(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        filters=[{"field": "developer", "value": "Ruhail", "match_type": "exact"}],
        return_fields=None,
        limit=50,
        schema_config={"data_start_row": 3},
        column_map={},
        service=mock_service
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["rows"][0]["RICEFW ID"] == "SD-001"


# 10. Same regression, for summarize's count_by_field branch.
@pytest.mark.asyncio
async def test_summarize_count_by_field_resolves_trailing_space_column():
    """Verify group-by on a natural-language term resolves against a trailing-space header."""
    mock_service = _mock_service_for_sheet(
        headers_row=["RICEFW ID", "Module", "Technical Resource "],
        data_rows=[
            ["SD-001", "SD", "Ruhail"],
            ["SD-002", "SD", "Ruhail"],
            ["SD-003", "SD", "Alex"],
        ]
    )

    result = await summarize(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        args={"report_type": "count_by_field", "group_by_field": "developer"},
        schema_config={"data_start_row": 3},
        column_map={},
        service=mock_service
    )

    assert result["ok"] is True
    breakdown = {b["value"]: b["count"] for b in result["breakdown"]}
    assert breakdown["Ruhail"] == 2
    assert breakdown["Alex"] == 1


# 11. Regression for TDD §16.2: summarize's overdue report must read
# overdue_status_exclusions from args instead of a hard-coded status set.
@pytest.mark.asyncio
async def test_summarize_overdue_honors_status_exclusions_arg():
    """Verify a caller-supplied exclusion list actually excludes a status from 'overdue'."""
    mock_service = _mock_service_for_sheet(
        headers_row=["RICEFW ID", "Module", "Dev Status", "Go-Live Date"],
        data_rows=[
            ["SD-001", "SD", "In Review", "01/01/2020"],
            ["SD-002", "SD", "Not Started", "01/01/2020"],
        ]
    )

    result = await summarize(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        args={"report_type": "overdue", "overdue_status_exclusions": ["In Review"]},
        schema_config={"data_start_row": 3},
        column_map={},
        service=mock_service
    )

    assert result["ok"] is True
    ids = [item["id"] for item in result["items"]]
    assert "SD-001" not in ids
    assert "SD-002" in ids


# 12. Regression for TDD §16.1: data_quality must dispatch on check_type instead of always
# running every check, and must honor scope_module and fields.
@pytest.mark.asyncio
async def test_data_quality_check_type_blank_fields_only():
    """Verify check_type='blank_fields' runs only that check and skips DB-backed checks."""
    mock_service = _mock_service_for_sheet(
        headers_row=["RICEFW ID", "Module", "Dev Status"],
        data_rows=[
            ["SD-001", "SD", "Done"],
            ["SD-002", "SD", ""],
        ]
    )
    mock_db = AsyncMock()

    result = await run_data_quality_check(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        args={"check_type": "blank_fields", "fields": ["Dev Status"]},
        schema_config={"data_start_row": 3},
        db_session=mock_db,
        service=mock_service
    )

    assert result["ok"] is True
    assert result["check_type"] == "blank_fields"
    assert result["blank_field_counts"] == {"Dev Status": 1}
    assert "alerts" not in result
    assert "completeness_score" not in result
    assert "stale_items" not in result
    assert not mock_db.execute.called


# 13. Regression for TDD §4.1 (Phase 4): the Google refresh token must reach the queued
# job payload, not just the access token — otherwise a worker that picks up the job
# after the ~1h access token expires can't rebuild a self-refreshing Sheets client.
@pytest.mark.asyncio
async def test_write_job_carries_refresh_token():
    """Verify enqueue_write_job serializes google_refresh_token into the job payload."""
    mock_redis = AsyncMock()

    with patch("app.queue.producer.redis_client", mock_redis):
        job = await enqueue_write_job(
            user_email="test@example.com",
            google_access_token="access-tok",
            google_refresh_token="refresh-tok",
            session_id=None,
            tool_name="update_cell",
            spreadsheet_id="sheet-123",
            sheet_tab="SD",
            args={"ricefw_id": "SD-002", "updates": [{"field": "Dev Status", "value": "Done"}]},
            old_values={}
        )

    assert job.id is not None
    args, _ = mock_redis.rpush.call_args
    _, raw_payload = args
    payload_dict = json.loads(raw_payload)
    assert payload_dict["payload"]["google_access_token"] == "access-tok"
    assert payload_dict["payload"]["google_refresh_token"] == "refresh-tok"
