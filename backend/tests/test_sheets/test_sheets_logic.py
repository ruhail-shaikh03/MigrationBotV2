import pytest
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from googleapiclient.errors import HttpError

from app.sheets.retry import _with_retry
from app.sheets.read import find_row_num
from app.queue.producer import enqueue_write_job
from app.queue.worker import start_worker

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
