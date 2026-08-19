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
from app.api.chat import list_user_projects
from tests.conftest import require_test_database

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
async def setup_integration_db():
    require_test_database()
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

    # Mock dynamic row detection. A list, because the write path resolves every row an
    # ID matches and refuses when there is more than one — a single row number here is
    # the unambiguous case this test is about.
    mock_publish = AsyncMock()
    with patch("app.queue.worker.build_sheets_service", return_value=mock_sheets_service), \
         patch("app.queue.worker.publish_queue_update", mock_publish), \
         patch("app.sheets.write.find_all_row_nums", return_value=[4]):

        # Execute the queue processing logic
        await process_job(job_envelope["job_id"], job_envelope["payload"])

    # The client must be told the job finished, or the UI shows no feedback at all
    assert mock_publish.called
    notify = mock_publish.call_args.kwargs
    assert notify["status"] == "completed"
    assert notify["job_id"] == job.id
    assert notify["tool_name"] == "update_cell"
    assert notify["user_email"] == user.email
    assert notify["args"]["ricefw_id"] == "SD-002"

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


async def test_worker_notifies_client_on_failure(db_session: AsyncSession):
    """A write that blows up mid-flight must still surface a 'failed' queue_update."""
    project = Project(
        project_name="Failure project",
        spreadsheet_id="spreadsheet-failure-123",
        default_tab="SD",
        company_prefix="FF",
        schema_config={"data_start_row": 3, "primary_id_position": "B"}
    )
    db_session.add(project)
    await db_session.commit()

    job_payload = {
        "user_email": "failing@example.com",
        "google_access_token": "mock-token-123",
        "session_id": None,
        "tool_name": "update_cell",
        "spreadsheet_id": project.spreadsheet_id,
        "sheet_tab": "SD",
        "args": {"ricefw_id": "SD-009", "updates": [{"field": "Dev Status", "value": "Done"}]},
        "old_values": {}
    }

    mock_publish = AsyncMock()
    with patch("app.queue.worker.build_sheets_service", return_value=MagicMock()), \
         patch("app.queue.worker.update_cell", AsyncMock(side_effect=RuntimeError("Sheets API exploded"))), \
         patch("app.queue.worker.publish_queue_update", mock_publish):

        await process_job("job-failure-1", job_payload)

    assert mock_publish.called
    notify = mock_publish.call_args.kwargs
    assert notify["status"] == "failed"
    assert notify["job_id"] == "job-failure-1"
    assert "Sheets API exploded" in notify["error"]


async def test_add_row_ignores_client_supplied_id_and_uses_sequential_assignment(db_session: AsyncSession):
    """Regression for TDD §16.15: add_row must always compute the RICEFW ID server-side via
    next_ricefw_id. The tool schema never exposes ricefw_id/prefix to the model, so if either
    sneaks into args (a bug, or a future unreviewed change), the worker must still ignore them
    rather than silently honoring an unvalidated client-supplied ID."""
    project = Project(
        project_name="Add Row Project",
        spreadsheet_id="spreadsheet-addrow-123",
        default_tab="SD",
        company_prefix="FF",
        schema_config={"data_start_row": 3, "primary_id_position": "B"}
    )
    db_session.add(project)
    await db_session.commit()

    job_payload = {
        "user_email": "adder@example.com",
        "google_access_token": "mock-token-123",
        "session_id": None,
        "tool_name": "add_row",
        "spreadsheet_id": project.spreadsheet_id,
        "sheet_tab": "SD",
        "args": {
            "module": "SD",
            "type": "R",
            "description": "New report",
            "ricefw_id": "SD-999",
            "prefix": "ZZ",
        },
        "old_values": {}
    }

    mock_publish = AsyncMock()
    with patch("app.queue.worker.build_sheets_service", return_value=MagicMock()), \
         patch("app.sheets.meta.next_ricefw_id", AsyncMock(return_value="FF-SD-004")) as mock_next_id, \
         patch("app.queue.worker.add_row", AsyncMock(return_value={"ok": True, "added": "FF-SD-004"})) as mock_add_row, \
         patch("app.queue.worker.publish_queue_update", mock_publish):

        await process_job("job-addrow-1", job_payload)

    assert mock_next_id.called
    assert mock_next_id.call_args.kwargs["prefix"] is None

    assert mock_add_row.called
    assert mock_add_row.call_args.kwargs["ricefw_id"] == "FF-SD-004"

    assert mock_publish.called
    assert mock_publish.call_args.kwargs["status"] == "completed"


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


async def test_list_user_projects_filters_to_permitted_projects(db_session: AsyncSession):
    """Regression for TDD §16.2: GET /api/projects must only return projects the caller
    has an explicit permissions row for — previously every authenticated caller saw
    every active project, including ones nobody had granted them access to."""
    user = User(email="regular@example.com", display_name="Regular User", google_sub="sub-regular-1")
    granted_project = Project(
        project_name="Granted Project",
        spreadsheet_id="spreadsheet-granted-1",
        default_tab="SD",
        company_prefix="FF"
    )
    ungranted_project = Project(
        project_name="Ungranted Project",
        spreadsheet_id="spreadsheet-ungranted-1",
        default_tab="SD",
        company_prefix="FF"
    )
    db_session.add_all([user, granted_project, ungranted_project])
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(granted_project)
    await db_session.refresh(ungranted_project)

    db_session.add(Permission(
        user_id=user.id,
        project_id=granted_project.id,
        role="editor",
        allowed_fields=["*"],
        denied_operations=[]
    ))
    await db_session.commit()

    projects = await list_user_projects(db=db_session, current_user=user)

    project_ids = {p["id"] for p in projects}
    assert granted_project.id in project_ids
    assert ungranted_project.id not in project_ids
