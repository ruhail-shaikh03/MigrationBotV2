import pytest
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.engine import init_db, drop_db, AsyncSessionLocal
from app.models.user import User
from app.models.project import Project
from app.models.permission import Permission
from app.models.session import Session as UserSession
from app.models.audit_log import AuditLog
from app.queue.producer import enqueue_write_job
from app.queue.worker import process_job
from app.core.agentic_loop import run_agentic_loop
from app.core.permissions import PermissionChecker

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
async def setup_integration_db():
    # Setup test database tables before each test and teardown after
    try:
        await drop_db()
    except Exception:
        pass
    await init_db()
    yield
    await drop_db()

@pytest.fixture
async def db_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

async def test_e2e_write_flow(db_session: AsyncSession):
    # 1. Populate initial Database records (User, Project, UserSession)
    user = User(
        email="integration@example.com",
        display_name="Integration tester",
        google_sub="sub-integration-1"
    )
    project = Project(
        project_name="Integration project",
        spreadsheet_id="spreadsheet-integration-123",
        default_tab="SD",
        company_prefix="FF",
        schema_config={
            "primary_id_position": "B",
            "data_start_row": 3,
            "status_column": "Dev Status"
        }
    )
    db_session.add_all([user, project])
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(project)

    user_sess = UserSession(
        user_id=user.id,
        project_id=project.id,
        active_tab="SD"
    )
    db_session.add(user_sess)
    await db_session.commit()
    await db_session.refresh(user_sess)

    # 2. Enqueue the write job
    mock_redis = AsyncMock()
    
    with patch("app.queue.producer.redis_client", mock_redis):
        job = await enqueue_write_job(
            user_email=user.email,
            google_access_token="mock-token-123",
            session_id=user_sess.id,
            tool_name="update_cell",
            spreadsheet_id=project.spreadsheet_id,
            sheet_tab="SD",
            args={"ricefw_id": "SD-002", "updates": [{"field": "Dev Status", "value": "Done"}]},
            old_values={"Dev Status": "In Progress"}
        )
    
    assert job.id is not None
    assert mock_redis.rpush.called

    # 3. Define the job envelope to simulate queue extraction
    job_envelope = {
        "job_id": job.id,
        "payload": {
            "user_email": user.email,
            "google_access_token": "mock-token-123",
            "session_id": str(user_sess.id),
            "tool_name": "update_cell",
            "spreadsheet_id": project.spreadsheet_id,
            "sheet_tab": "SD",
            "args": {"ricefw_id": "SD-002", "updates": [{"field": "Dev Status", "value": "Done"}]},
            "old_values": {"Dev Status": "In Progress"}
        }
    }

    # 4. Mock the Sheets API write execution
    mock_sheets_service = MagicMock()
    mock_values = MagicMock()
    mock_values.update.return_value.execute.return_value = {"updatedCells": 1}
    mock_values.get.return_value.execute.return_value = {"values": [["RICEFW ID", "Dev Status", "Business Owner"]]}
    mock_sheets_service.spreadsheets.return_value.values.return_value = mock_values

    # Mock dynamic row detection (find_row_num)
    with patch("app.queue.worker.build_sheets_service", return_value=mock_sheets_service), \
         patch("app.sheets.write.find_row_num", return_value=4):
        
        # Execute the queue processing logic
        await process_job(job_envelope["job_id"], job_envelope["payload"])

    # 5. Verify the Audit Log is updated in the database
    async with AsyncSessionLocal() as fresh_session:
        stmt = select(AuditLog).where(AuditLog.user_email == user.email)
        result = await fresh_session.execute(stmt)
        audit_records = result.scalars().all()
        
        assert len(audit_records) == 1
        record = audit_records[0]
        assert record.tool_name == "update_cell"
        assert record.ricefw_id == "SD-002"
        assert record.field == "Dev Status"
        assert record.old_value == "In Progress"
        assert record.new_value == "Done"
        assert record.result_ok is True


async def test_e2e_read_flow(db_session: AsyncSession):
    # 1. Initialize user, project, permission profiles
    user = User(
        email="reader@example.com",
        display_name="Reader tester",
        google_sub="sub-reader-1"
    )
    project = Project(
        project_name="Read Test Project",
        spreadsheet_id="spreadsheet-read-123",
        default_tab="SD",
        company_prefix="FF"
    )
    db_session.add_all([user, project])
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(project)

    # Mock the LLM client
    mock_llm_client = AsyncMock()
    
    # Simulate first turn tool call from LLM
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_read_1"
    mock_tool_call.type = "function"
    mock_tool_call.function.name = "get_row"
    mock_tool_call.function.arguments = '{"ricefw_id": "SD-001"}'

    mock_msg_turn1 = MagicMock()
    mock_msg_turn1.content = "Let me read the row first."
    mock_msg_turn1.tool_calls = [mock_tool_call]
    mock_choice_turn1 = MagicMock()
    mock_choice_turn1.message = mock_msg_turn1
    
    mock_response_turn1 = MagicMock()
    mock_response_turn1.choices = [mock_choice_turn1]

    # Simulate second turn text summary from LLM
    mock_msg_turn2 = MagicMock()
    mock_msg_turn2.content = "The status of SD-001 is In Progress."
    mock_msg_turn2.tool_calls = None
    mock_choice_turn2 = MagicMock()
    mock_choice_turn2.message = mock_msg_turn2
    
    mock_response_turn2 = MagicMock()
    mock_response_turn2.choices = [mock_choice_turn2]

    # Sequence responses
    mock_llm_client.chat.completions.create.side_effect = [mock_response_turn1, mock_response_turn2]

    # Mock WebSocket helper
    sent_ws_messages = []
    async def mock_send(msg):
        sent_ws_messages.append(msg)

    # Mock the Sheets Read executor (get_row)
    mock_sheets_row = {
        "RICEFW ID": "SD-001",
        "Dev Status": "In Progress",
        "Business Owner": "Ruhail"
    }
    
    # We patch dispatch_tool so it intercepts the "get_row" tool call and returns our mocked sheets data
    with patch("app.core.agentic_loop.dispatch_tool", AsyncMock(return_value=mock_sheets_row)):
        checker = PermissionChecker(user.email, "viewer", ["*"], [])
        
        await run_agentic_loop(
            user_message="Find details on SD-001",
            message_history=[],
            user_email=user.email,
            session_id="session-id-123",
            spreadsheet_id=project.spreadsheet_id,
            active_tab="SD",
            schema_config={},
            column_map={},
            checker=checker,
            llm_client=mock_llm_client,
            send_websocket_msg=mock_send,
            db_session=db_session,
            max_iterations=5
        )

    # Check WS stream outputs
    tool_starts = [m for m in sent_ws_messages if m.get("type") == "tool_start"]
    assistant_replies = [m for m in sent_ws_messages if m.get("type") == "assistant"]

    assert len(tool_starts) == 1
    assert tool_starts[0]["tool"] == "get_row"
    assert tool_starts[0]["args"] == {"ricefw_id": "SD-001"}

    assert len(assistant_replies) == 1
    assert "In Progress" in assistant_replies[0]["content"]
