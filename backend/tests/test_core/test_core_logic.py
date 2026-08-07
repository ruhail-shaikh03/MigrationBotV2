import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from jose import jwt
from app.config import settings
from app.core.llm_router import select_model
from app.core.permissions import PermissionChecker, get_user_permissions
from app.core import audit
from app.core.agentic_loop import run_agentic_loop
from app.core.data_quality import DataQualityChecker

# Test cases below

# 1. JWT verification test
def test_jwt_verification_mock():
    """Verify that NextAuth JWT can be decoded with our JWT secret."""
    token_claims = {
        "email": "test@example.com",
        "name": "Test User",
        "picture": "https://example.com/pic.png",
        "sub": "google-sub-123"
    }
    token = jwt.encode(token_claims, settings.JWT_SECRET, algorithm="HS256")
    
    decoded = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    assert decoded["email"] == "test@example.com"
    assert decoded["name"] == "Test User"
    assert decoded["sub"] == "google-sub-123"


# 2. LLM router complexity selection test
def test_llm_routing():
    """Verify conditional prompts route to reasoner and standard route to chat."""
    # Conditionals route to deepseek-reasoner on Iteration 0
    messages_conditional = [{"role": "user", "content": "If SD-045 is completed, set dev status to Done."}]
    model = select_model(0, messages_conditional)
    assert model == "deepseek-reasoner"
    
    # Standard prompts route to deepseek-chat on Iteration 0
    messages_standard = [{"role": "user", "content": "Show me all active SD objects."}]
    model = select_model(0, messages_standard)
    assert model == "deepseek-chat"
    
    # Subsequent iterations (> 0) always fallback to deepseek-chat
    model = select_model(1, messages_conditional)
    assert model == "deepseek-chat"


# 3. RBAC Interception logic test
def test_rbac_interception():
    """Verify PermissionChecker enforces roles, column bounds, and operation blocks."""
    # Test Viewer access (Read tools allowed; write tools blocked)
    viewer_checker = PermissionChecker("viewer@example.com", role="viewer", allowed_fields=["*"], denied_operations=[])
    
    allowed, reason = viewer_checker.can_execute("get_row", {"ricefw_id": "SD-012"})
    assert allowed is True
    assert reason == ""

    allowed, reason = viewer_checker.can_execute("update_cell", {"ricefw_id": "SD-012"})
    assert allowed is False
    assert "read-only access" in reason

    # Test Editor tool blocks
    editor_checker = PermissionChecker("editor@example.com", role="editor", allowed_fields=["*"], denied_operations=["add_row"])
    allowed, reason = editor_checker.can_execute("add_row", {})
    assert allowed is False
    assert "don't have permission to run `add_row`" in reason

    # Test Editor column list bounds
    field_editor = PermissionChecker("editor@example.com", role="editor", allowed_fields=["Dev Status"], denied_operations=[])
    
    # Update allowed field
    allowed, reason = field_editor.can_execute("update_cell", {"updates": [{"field": "Dev Status", "value": "Ready for UAT"}]})
    assert allowed is True
    
    # Update blocked field
    allowed, reason = field_editor.can_execute("update_cell", {"updates": [{"field": "Business Owner", "value": "Alex"}]})
    assert allowed is False
    assert "don't have write access to: **Business Owner**" in reason


# 4. Agentic Loop max iteration cap (8) test
@pytest.mark.asyncio
async def test_agentic_loop_max_iterations():
    """Verify agentic loop forcefully breaks at 8 iterations if LLM continues requesting tools."""
    mock_client = AsyncMock()
    
    # Mock endless tool call from LLM response
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_mock_id"
    mock_tool_call.type = "function"
    mock_tool_call.function.name = "get_row"
    mock_tool_call.function.arguments = '{"ricefw_id": "SD-012"}'
    
    mock_message = MagicMock()
    mock_message.content = "I need to run get_row again."
    mock_message.tool_calls = [mock_tool_call]
    
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    
    mock_client.chat.completions.create.return_value = mock_response
    
    # Mock WebSocket send callback
    sent_messages = []
    async def mock_send(msg):
        sent_messages.append(msg)
        
    checker = PermissionChecker("admin@example.com", "admin", ["*"], [])
    
    # Run the loop with max_iterations=8 (default)
    history = await run_agentic_loop(
        user_message="Track SD-012",
        message_history=[],
        user_email="admin@example.com",
        session_id="session-123",
        spreadsheet_id="spread-123",
        active_tab="SD",
        schema_config={},
        column_map={},
        checker=checker,
        llm_client=mock_client,
        send_websocket_msg=mock_send,
        db_session=None,
        max_iterations=8
    )
    
    # Check that it sent tool starts 8 times and then exited
    tool_starts = [msg for msg in sent_messages if msg["type"] == "tool_start"]
    assert len(tool_starts) == 8


# 4b. Regression for TDD §16.3: exhausting the iteration cap must notify the client
# instead of returning silently (the for-loop previously had no else: clause, so the
# UI's spinner never resolved to a final "assistant" or "error" frame).
@pytest.mark.asyncio
async def test_agentic_loop_iteration_cap_notifies_client():
    """Verify hitting max_iterations sends exactly one terminal error frame, last in the stream."""
    mock_client = AsyncMock()

    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_mock_id"
    mock_tool_call.type = "function"
    mock_tool_call.function.name = "get_row"
    mock_tool_call.function.arguments = '{"ricefw_id": "SD-012"}'

    mock_message = MagicMock()
    mock_message.content = "I need to run get_row again."
    mock_message.tool_calls = [mock_tool_call]

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client.chat.completions.create.return_value = mock_response

    sent_messages = []
    async def mock_send(msg):
        sent_messages.append(msg)

    checker = PermissionChecker("admin@example.com", "admin", ["*"], [])

    with patch("app.core.agentic_loop.dispatch_tool", AsyncMock(return_value={"ok": True, "data": {}})):
        await run_agentic_loop(
            user_message="Track SD-012",
            message_history=[],
            user_email="admin@example.com",
            session_id="session-123",
            spreadsheet_id="spread-123",
            active_tab="SD",
            schema_config={},
            column_map={},
            checker=checker,
            llm_client=mock_client,
            send_websocket_msg=mock_send,
            db_session=None,
            max_iterations=3
        )

    errors = [msg for msg in sent_messages if msg["type"] == "error"]
    assert len(errors) == 1
    assert "3 steps" in errors[0]["message"]
    assert sent_messages[-1]["type"] == "error"


# 5. Non-blocking audit logger test
@pytest.mark.asyncio
async def test_audit_logger_nonblocking():
    """Verify audit logger database insert failures are caught and do not bubble up."""
    mock_session = MagicMock()
    # Mock the database context manager to return a session that fails on commit
    mock_session.__aenter__.return_value = mock_session
    mock_session.commit.side_effect = Exception("PostgreSQL down: connection timeout")
    
    # Patch the AsyncSessionLocal factory to return our mock session
    with patch("app.core.audit.AsyncSessionLocal", return_value=mock_session):
        task = audit._write_audit_record(
            user_email="test@example.com",
            session_id=None,
            tool_name="update_cell",
            spreadsheet_id="sheet-123",
            sheet_tab="SD",
            ricefw_id="SD-012",
            field="Dev Status",
            old_value="In Progress",
            new_value="Done",
            args={},
            result_ok=True,
            error=None
        )
        
        # Await the task. It should catch the Exception, log it, and complete cleanly without throwing.
        try:
            await task
            exception_raised = False
        except Exception:
            exception_raised = True

        assert exception_raised is False


# 6. Regression for TDD §16.3: DataQualityChecker's assignee-column lookup used
# `if not self._get_col_idx(...)` which treats a valid index 0 as "not found".
def test_data_quality_checker_handles_assignee_at_column_zero():
    """Verify a sheet whose assignee column is physically first (index 0) is not silently
    swapped for a non-existent 'Assigned To' fallback."""
    headers = ["Technical Resource ", "RICEFW ID", "Dev Status"]
    rows = [
        ["dev@example.com", "SD-001", "Completed"],
        ["", "SD-002", "Completed"],
    ]
    schema = {
        "assignee_column": "Technical Resource ",
        "primary_id_column": "RICEFW ID",
        "status_column": "Dev Status"
    }
    checker = DataQualityChecker(headers, rows, schema)

    # completeness_score's default critical_fields must include the assignee column;
    # if it were dropped (the bug), all remaining fields are filled and the score reads 100.0.
    score = checker.completeness_score()
    assert score == 83.3

    # consistency_checks' unregistered-assignee rule must actually run against column 0.
    alerts = checker.consistency_checks(valid_emails=["someone-else@example.com"])
    messages = [a["message"] for a in alerts]
    assert any("not registered in permissions" in m for m in messages)


# 7. Regression for TDD (prior §16.8, "optimistic write result misleads the model"): a
# queued write result must never be fed back to the model as if it already succeeded.
@pytest.mark.asyncio
async def test_agentic_loop_flags_queued_write_as_pending():
    """Verify a queued (not-yet-applied) write result is annotated in the tool message
    content — this is the only place the note reliably reaches the model, since the
    compact system prompt used from iteration 1 onward drops the RULES section."""
    mock_client = AsyncMock()

    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_bulk_1"
    mock_tool_call.type = "function"
    mock_tool_call.function.name = "bulk_update"
    mock_tool_call.function.arguments = '{"set_field": "Dev Status", "set_value": "Done"}'

    mock_msg_turn1 = MagicMock()
    mock_msg_turn1.content = "Updating rows."
    mock_msg_turn1.tool_calls = [mock_tool_call]
    mock_choice_turn1 = MagicMock()
    mock_choice_turn1.message = mock_msg_turn1
    mock_response_turn1 = MagicMock()
    mock_response_turn1.choices = [mock_choice_turn1]

    mock_msg_turn2 = MagicMock()
    mock_msg_turn2.content = "Queued the update — I'll confirm once it's applied."
    mock_msg_turn2.tool_calls = None
    mock_choice_turn2 = MagicMock()
    mock_choice_turn2.message = mock_msg_turn2
    mock_response_turn2 = MagicMock()
    mock_response_turn2.choices = [mock_choice_turn2]

    mock_client.chat.completions.create.side_effect = [mock_response_turn1, mock_response_turn2]

    sent_messages = []
    async def mock_send(msg):
        sent_messages.append(msg)

    checker = PermissionChecker("editor@example.com", "editor", ["*"], [])

    queued_result = {
        "ok": True,
        "status": "queued",
        "job_id": "job-1",
        "message": "Operation queued successfully. Job ID: job-1"
    }

    with patch("app.core.agentic_loop.dispatch_tool", AsyncMock(return_value=queued_result)):
        history = await run_agentic_loop(
            user_message="Set all SD items to Done",
            message_history=[],
            user_email="editor@example.com",
            session_id="session-123",
            spreadsheet_id="spread-123",
            active_tab="SD",
            schema_config={},
            column_map={},
            checker=checker,
            llm_client=mock_client,
            send_websocket_msg=mock_send,
            db_session=None,
            max_iterations=5
        )

    tool_messages = [m for m in history if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "[System Note]" in tool_messages[0]["content"]
    assert "not yet applied" in tool_messages[0]["content"]
    # A failure-recovery note must never appear alongside a successful queued result.
    assert "[System Recovery Note]" not in tool_messages[0]["content"]


# 8. Regression for TDD §16.3 (Phase 4): a caller get_user_permissions can't place —
# here, no project_id — must fail closed to settings.DEFAULT_ROLE ("viewer"), not the
# old hardcoded "editor" fallback.
@pytest.mark.asyncio
async def test_get_user_permissions_fails_closed_without_project_id():
    """No project_id short-circuits before any DB access, so db=None is safe here."""
    assert settings.DEFAULT_ROLE == "viewer"

    checker = await get_user_permissions(db=None, email="nobody@example.com", project_id=None)

    assert checker.role == "viewer"
    allowed, reason = checker.can_execute("update_cell", {})
    assert allowed is False
    assert reason


# 9. Regression for TDD §16.4 (Phase 4): the mock-token/'@' dev-auth fallback must not
# authenticate when ALLOW_DEV_AUTH is off (the default).
@pytest.mark.asyncio
async def test_dev_auth_bypass_gated_behind_allow_dev_auth():
    from fastapi import HTTPException
    from app.deps import get_current_user

    assert settings.ALLOW_DEV_AUTH is False

    class FakeCredentials:
        credentials = "someone@example.com"  # not a valid JWT; contains '@'

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=FakeCredentials(), db=AsyncMock())

    assert exc_info.value.status_code == 401
