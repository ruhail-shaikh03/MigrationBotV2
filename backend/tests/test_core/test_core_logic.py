import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from jose import jwt
from app.config import settings
from app.core.llm_router import select_model
from app.core.permissions import PermissionChecker
from app.core import audit
from app.core.agentic_loop import run_agentic_loop

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
