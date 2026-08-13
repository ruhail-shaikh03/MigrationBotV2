import pytest
import json
import asyncio
import redis
from unittest.mock import MagicMock, AsyncMock, patch
from googleapiclient.errors import HttpError

from app.sheets.retry import _with_retry
from app.sheets.read import find_row_num, get_bulk_rows_raw, search_rows, summarize, run_data_quality_check
from app.queue.producer import enqueue_write_job
from app.queue.worker import (
    start_worker, recover_stale_jobs, QUEUE_KEY, PROCESSING_KEY, DEAD_LETTER_KEY,
    MAX_ATTEMPTS, REDIS_BACKOFF_INITIAL_SECONDS,
)
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
    mock_redis.get.return_value = None   # no existing job-state for _set_job_state to merge into
    mock_redis.lpop.return_value = None  # recover_stale_jobs: processing list starts empty

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

    async def mock_blmove(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return json.dumps(job_envelope)
        else:
            raise asyncio.CancelledError("Stop loop")

    mock_redis.blmove.side_effect = mock_blmove

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
        # A clean pass (process_job didn't raise) must remove the job from the
        # processing list it was BLMOVE'd into on pickup.
        assert mock_redis.lrem.called


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


# 14. Regression for TDD §16.1: a job left in the processing list by a worker that
# crashed mid-job (BLMOVE puts it there atomically on pickup) must be requeued, not
# lost, as long as it hasn't exceeded MAX_ATTEMPTS.
@pytest.mark.asyncio
async def test_recover_stale_jobs_requeues_under_max_attempts():
    mock_redis = AsyncMock()
    stale_envelope = {
        "job_id": "job-stale-1",
        "attempt": 0,
        "payload": {"user_email": "a@b.com", "tool_name": "update_cell", "args": {}, "session_id": None}
    }
    mock_redis.lpop.side_effect = [json.dumps(stale_envelope), None]

    recovered = await recover_stale_jobs(mock_redis)

    assert recovered == 1
    rpush_calls = [c for c in mock_redis.rpush.call_args_list if c[0][0] == QUEUE_KEY]
    assert len(rpush_calls) == 1
    requeued = json.loads(rpush_calls[0][0][1])
    assert requeued["job_id"] == "job-stale-1"
    assert requeued["attempt"] == 1  # incremented from 0


# 15. Same recovery path, but past MAX_ATTEMPTS: must dead-letter and notify the
# user instead of retrying forever or silently dropping the write.
@pytest.mark.asyncio
async def test_recover_stale_jobs_dead_letters_past_max_attempts():
    mock_redis = AsyncMock()
    stale_envelope = {
        "job_id": "job-stale-2",
        "attempt": MAX_ATTEMPTS,  # one more crash pushes it over the limit
        "payload": {"user_email": "a@b.com", "tool_name": "update_cell", "args": {}, "session_id": None}
    }
    mock_redis.lpop.side_effect = [json.dumps(stale_envelope), None]

    with patch("app.queue.worker.publish_queue_update", AsyncMock()) as mock_publish:
        recovered = await recover_stale_jobs(mock_redis)

    assert recovered == 1
    dead_letter_calls = [c for c in mock_redis.rpush.call_args_list if c[0][0] == DEAD_LETTER_KEY]
    assert len(dead_letter_calls) == 1
    queue_calls = [c for c in mock_redis.rpush.call_args_list if c[0][0] == QUEUE_KEY]
    assert len(queue_calls) == 0  # must NOT go back onto the live queue

    assert mock_publish.called
    assert mock_publish.call_args.kwargs["status"] == "failed"
    assert mock_publish.call_args.kwargs["job_id"] == "job-stale-2"


# 15b. Regression for the 2026-08 incident: a Redis that answers BLMOVE with
# "READONLY You can't write against a read only replica" must make the worker back
# off in place, not die. ReadOnlyError is a ResponseError, NOT a ConnectionError, so
# it slipped past the TimeoutError-only handler; the loop crashed, the container
# restarted, and every restart burned one MAX_ATTEMPTS budget on innocent jobs.
@pytest.mark.asyncio
async def test_worker_survives_readonly_replica_error():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.lpop.return_value = None  # nothing stale to recover

    call_count = 0

    async def mock_blmove(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise redis.exceptions.ReadOnlyError("You can't write against a read only replica.")
        raise asyncio.CancelledError("Stop loop")

    mock_redis.blmove.side_effect = mock_blmove

    with patch("app.queue.worker.aioredis.from_url", return_value=mock_redis), \
         patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        try:
            await start_worker()
        except asyncio.CancelledError:
            pass

    # Survived both READONLY replies and kept polling rather than exiting.
    assert call_count == 3
    slept = [c[0][0] for c in mock_sleep.call_args_list]
    assert slept[:2] == [REDIS_BACKOFF_INITIAL_SECONDS, REDIS_BACKOFF_INITIAL_SECONDS * 2]


# 15c. A Redis outage during startup recovery must not prevent the worker from
# reaching its main loop — the loop retries, and the next start re-runs recovery.
@pytest.mark.asyncio
async def test_worker_starts_when_stale_job_recovery_fails():
    mock_redis = AsyncMock()
    mock_redis.lpop.side_effect = redis.exceptions.ConnectionError("connection refused")
    mock_redis.blmove.side_effect = asyncio.CancelledError("Stop loop")

    with patch("app.queue.worker.aioredis.from_url", return_value=mock_redis):
        try:
            await start_worker()
        except asyncio.CancelledError:
            pass

    assert mock_redis.blmove.called  # reached the main loop despite recovery failing


# 16. Regression: the job_id dispatch_tool returns to the caller must be queryable
# immediately at enqueue time, not just after the worker eventually picks it up.
@pytest.mark.asyncio
async def test_enqueue_write_job_sets_initial_job_state():
    from app.queue.schemas import JOB_STATE_PREFIX

    mock_redis = AsyncMock()
    with patch("app.queue.producer.redis_client", mock_redis):
        job = await enqueue_write_job(
            user_email="test@example.com",
            google_access_token="tok",
            session_id=None,
            tool_name="update_cell",
            spreadsheet_id="sheet-123",
            sheet_tab="SD",
            args={},
            old_values={}
        )

    state_calls = [c for c in mock_redis.set.call_args_list if c[0][0] == f"{JOB_STATE_PREFIX}:{job.id}"]
    assert len(state_calls) == 1
    state = json.loads(state_calls[0][0][1])
    assert state["status"] == "queued"
    assert state["user_email"] == "test@example.com"
    assert state["job_id"] == job.id


# 17. Regression for TDD §16.3: a tracker larger than one page must not be silently
# truncated — previously search_rows/summarize/data_quality each did a single fixed
# `{start}:{start+2000}` fetch. page_size is dialed down to keep the fixture small
# while still exercising real multi-page pagination and aggregation.
@pytest.mark.asyncio
async def test_fetch_all_rows_paginates_past_a_single_page():
    from app.sheets.read import _fetch_all_rows

    pages = [
        [["r1"], ["r2"]],
        [["r3"], ["r4"]],
        [["r5"]],  # shorter than page_size — signals end of data
    ]
    mock_service = MagicMock()
    call_count = 0

    def mock_get(spreadsheetId, range):
        nonlocal call_count
        mock_exec = MagicMock()
        page = pages[call_count] if call_count < len(pages) else []
        call_count += 1
        mock_exec.execute.return_value = {"values": page}
        return mock_exec

    mock_service.spreadsheets.return_value.values.return_value.get.side_effect = mock_get

    rows, truncated = await _fetch_all_rows(mock_service, "sheet-123", "SD", data_start_row=3, page_size=2)

    assert truncated is False
    assert rows == [["r1"], ["r2"], ["r3"], ["r4"], ["r5"]]
    assert call_count == 3


# 18. The pagination safety valve must stop and report truncated=True rather than
# looping forever if a misconfigured schema causes rows to never run out.
@pytest.mark.asyncio
async def test_fetch_all_rows_respects_safety_cap():
    from app.sheets import read as read_module

    mock_service = MagicMock()

    def mock_get(spreadsheetId, range):
        mock_exec = MagicMock()
        # Always a full page — simulates data that never yields a short "end" page.
        mock_exec.execute.return_value = {"values": [["r"], ["r"]]}
        return mock_exec

    mock_service.spreadsheets.return_value.values.return_value.get.side_effect = mock_get

    with patch.object(read_module, "_MAX_SCAN_ROWS", 5):
        rows, truncated = await read_module._fetch_all_rows(
            mock_service, "sheet-123", "SD", data_start_row=3, page_size=2
        )

    assert truncated is True
    assert len(rows) == 5


# 19. Regression for the §11 bulk-read-cost callout: resolving N target IDs' current
# values must cost a small constant number of Sheets calls (one ID-column scan, one
# header fetch, one batchGet), not ~3 calls per ID.
@pytest.mark.asyncio
async def test_get_bulk_rows_raw_uses_one_batch_get_for_n_targets():
    mock_service = MagicMock()
    get_call_count = 0
    batch_get_call_count = 0

    headers_row = ["RICEFW ID", "Dev Status"]
    id_column_rows = [["SD-001"], ["SD-002"], ["SD-003"]]  # rows 3, 4, 5

    def mock_get(spreadsheetId, range):
        nonlocal get_call_count
        get_call_count += 1
        mock_exec = MagicMock()
        if range == "SD!2:2":
            mock_exec.execute.return_value = {"values": [headers_row]}
        elif range == "SD!B3:B":
            mock_exec.execute.return_value = {"values": id_column_rows}
        else:
            mock_exec.execute.return_value = {"values": []}
        return mock_exec

    def mock_batch_get(spreadsheetId, ranges):
        nonlocal batch_get_call_count
        batch_get_call_count += 1
        mock_exec = MagicMock()
        mock_exec.execute.return_value = {
            "valueRanges": [
                {"values": [["SD-001", "Done"]]},
                {"values": [["SD-002", "In Progress"]]},
            ]
        }
        return mock_exec

    mock_service.spreadsheets.return_value.values.return_value.get.side_effect = mock_get
    mock_service.spreadsheets.return_value.values.return_value.batchGet.side_effect = mock_batch_get

    result = await get_bulk_rows_raw(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        args={"ricefw_ids": ["SD-001", "SD-002"], "set_field": "Dev Status"},
        schema_config={"data_start_row": 3, "primary_id_position": "B"},
        service=mock_service
    )

    assert batch_get_call_count == 1  # one batchGet regardless of target count
    assert get_call_count == 2        # one header fetch + one ID-column scan, not one per ID
    assert result["SD-001"]["Dev Status"] == "Done"
    assert result["SD-002"]["Dev Status"] == "In Progress"


# 20. Same fix, the write side: bulk_update must resolve every target ID's row number
# from one ID-column scan, not one find_row_num call (its own full scan) per ID.
@pytest.mark.asyncio
async def test_bulk_update_resolves_rows_with_one_id_scan():
    from app.sheets.write import bulk_update

    mock_service = MagicMock()
    id_scan_call_count = 0

    headers_row = ["RICEFW ID", "Dev Status"]
    id_column_rows = [["SD-001"], ["SD-002"]]

    def mock_get(spreadsheetId, range):
        nonlocal id_scan_call_count
        mock_exec = MagicMock()
        if range == "SD!2:2":
            mock_exec.execute.return_value = {"values": [headers_row]}
        elif range == "SD!B3:B":
            id_scan_call_count += 1
            mock_exec.execute.return_value = {"values": id_column_rows}
        else:
            mock_exec.execute.return_value = {"values": []}
        return mock_exec

    mock_service.spreadsheets.return_value.values.return_value.get.side_effect = mock_get
    mock_service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.return_value = {}

    result = await bulk_update(
        service=mock_service,
        spreadsheet_id="sheet-123",
        sheet_tab="SD",
        args={"ricefw_ids": ["SD-001", "SD-002"], "set_field": "Dev Status", "set_value": "Done"},
        schema_config={"data_start_row": 3, "primary_id_position": "B"}
    )

    assert id_scan_call_count == 1  # one scan resolves both IDs, not one scan each
    assert result["ok"] is True
    assert set(result["succeeded"]) == {"SD-001", "SD-002"}


# 15. summarize(count_by_field) on a people column must split shared cells and apply the
# project's alias map, or chat reports "Minhaj Alam & Dawood" as a person while the
# dashboard beside it reports two — from the same sheet, on the same tab.
@pytest.mark.asyncio
async def test_summarize_count_by_field_splits_and_aliases_a_people_column():
    from app.core.aliases import PersonResolver

    mock_service = _mock_service_for_sheet(
        headers_row=["RICEFW ID", "Module", "Developer Name"],
        data_rows=[
            ["SD-001", "SD", "Minhaj Alam & Dawood"],
            ["SD-002", "SD", "Muhammad Dawood Umer"],
            ["SD-003", "SD", "Madiha"],
        ],
    )

    result = await summarize(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        args={"report_type": "count_by_field", "group_by_field": "Developer Name"},
        schema_config={
            "data_start_row": 3,
            "people_columns": [
                {"key": "developer_name", "label": "Developer Name", "header": "Developer Name"}
            ],
        },
        column_map={},
        service=mock_service,
        resolver=PersonResolver({
            "dawood": ["Muhammad Dawood Umer"],
            "madiha": ["Madiha Shah Bukhari"],
        }),
    )

    assert result["ok"] is True
    counts = {b["value"]: b["count"] for b in result["breakdown"]}
    # The composite is gone; Dawood is credited for both rows that name him.
    assert "Minhaj Alam & Dawood" not in counts
    assert counts["Muhammad Dawood Umer"] == 2
    assert counts["Minhaj Alam"] == 1
    assert counts["Madiha Shah Bukhari"] == 1


# 16. The same grouping on a NON-people column must be left exactly as it was: raw cell
# values, no splitting. An ampersand in a module name is not two modules.
@pytest.mark.asyncio
async def test_summarize_count_by_field_does_not_split_a_plain_column():
    from app.core.aliases import PersonResolver

    mock_service = _mock_service_for_sheet(
        headers_row=["RICEFW ID", "Module", "Developer Name"],
        data_rows=[["SD-001", "Sales & Distribution", "A"], ["SD-002", "Sales & Distribution", "B"]],
    )

    result = await summarize(
        spreadsheet_id="sheet-123",
        active_tab="SD",
        args={"report_type": "count_by_field", "group_by_field": "Module"},
        schema_config={
            "data_start_row": 3,
            "people_columns": [
                {"key": "developer_name", "label": "Developer Name", "header": "Developer Name"}
            ],
        },
        column_map={},
        service=mock_service,
        resolver=PersonResolver(),
    )

    assert result["ok"] is True
    assert {b["value"]: b["count"] for b in result["breakdown"]} == {"Sales & Distribution": 2}
