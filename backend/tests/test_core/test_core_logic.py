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
from app.core.errors import (
    classify_error, error_result, failure_note, ambiguous_id_result, AmbiguousRowError,
    AMBIGUOUS_ID, AUTH, INFRASTRUCTURE, NOT_FOUND, RATE_LIMIT, UPSTREAM,
)

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
    # A failure note must never appear alongside a successful queued result.
    # (failure_note() formats as "Tool 'x' failed (<kind>): ...")
    assert "failed (" not in tool_messages[0]["content"]


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


# 10. Regression for TDD §16.11 (Phase 6): WRITE_TOOLS was defined but can_execute never
# read it — classification was by exclusion, so an editor could run any unclassified tool
# name (fell through to the bottom `return True, ""`), and a legitimate new read tool
# omitted from READ_ONLY_TOOLS would be silently denied to viewers with a misleading
# "read-only access" message instead of a "not recognized" one.
def test_permissions_unclassified_tool_fails_closed_for_editor_and_viewer():
    editor = PermissionChecker("editor@example.com", role="editor", allowed_fields=["*"], denied_operations=[])
    allowed, reason = editor.can_execute("delete_everything", {})
    assert allowed is False
    assert "not a recognized tool" in reason

    viewer = PermissionChecker("viewer@example.com", role="viewer", allowed_fields=["*"], denied_operations=[])
    allowed, reason = viewer.can_execute("delete_everything", {})
    assert allowed is False
    assert "not a recognized tool" in reason

    # Row admin still bypasses classification entirely, per §6.2.
    admin = PermissionChecker("admin@example.com", role="admin", allowed_fields=["*"], denied_operations=[])
    allowed, reason = admin.can_execute("delete_everything", {})
    assert allowed is True


# 11. Regression for TDD §16.20 (Phase 6): the "tabs" in schema_config disambiguation was
# copy-pasted at seven call sites. core/schema.py is now the one implementation.
def test_get_tab_schema_and_available_tabs_resolve_both_shapes():
    from app.core.schema import get_tab_schema, get_available_tabs

    flat_schema = {"data_start_row": 3, "primary_id_position": "B"}
    assert get_tab_schema(flat_schema, "SD") == flat_schema
    # A flat, single-tab config has nowhere to switch to.
    assert get_available_tabs(flat_schema) == []

    multi_tab_schema = {
        "tabs": {"SD": {"data_start_row": 3}, "FI": {"data_start_row": 4}},
        "global": {"detected_tabs": ["SD", "FI"]}
    }
    assert get_tab_schema(multi_tab_schema, "SD") == {"data_start_row": 3}
    assert get_tab_schema(multi_tab_schema, "MISSING") == {}
    assert set(get_available_tabs(multi_tab_schema)) == {"SD", "FI"}

    # Tab names come from `tabs` itself, so a config with no `global` block still works.
    multi_tab_no_global = {"tabs": {"SD": {}, "FI": {}}}
    assert set(get_available_tabs(multi_tab_no_global)) == {"SD", "FI"}


# A legacy stored schema_config may still carry global.valid_modules from before the
# rename. It must be inert: tab names come from `tabs`, and nothing may resurrect that
# key as an allowlist of legal ID prefixes (the SLCM-0586 rejection).
def test_legacy_valid_modules_key_is_ignored():
    from app.core.schema import get_available_tabs

    legacy = {
        "tabs": {"Main Data Sheet": {"data_start_row": 3}},
        "global": {"valid_modules": ["FI", "MM", "SD"]}
    }
    assert get_available_tabs(legacy) == ["Main Data Sheet"]


# The prompt builders must never substitute a hardcoded module vocabulary. An empty tab
# list means "single tab", not "here are twelve SAP module codes you must choose from".
def test_system_prompts_carry_no_hardcoded_module_allowlist():
    from app.core.tool_schemas import get_system_prompt, get_system_prompt_compact

    full = get_system_prompt([], "{}")
    compact = get_system_prompt_compact([])

    for text in (full, compact):
        assert "FI,MM,SD" not in text
        assert "TRM" not in text
        assert "single tab" in text

    # Real tab names are passed through verbatim.
    assert "Main Data Sheet" in get_system_prompt(["Main Data Sheet"], "{}")


# The function-calling schemas are a harder constraint than prose: an `enum` or `pattern`
# is something the model physically cannot emit around. SLCM-0586 failed get_row's id
# pattern (4-letter prefix, 4-digit number) and every module enum.
def test_tool_schemas_do_not_constrain_ids_or_categories():
    import json as _json
    from app.core.tool_schemas import TOOLS

    by_name = {t["function"]["name"]: t["function"] for t in TOOLS}

    assert "pattern" not in by_name["get_row"]["parameters"]["properties"]["ricefw_id"]

    for tool, path in [
        ("add_row", ["module"]),
        ("add_row", ["type"]),
        ("summarize", ["scope_module"]),
        ("data_quality", ["scope_module"]),
    ]:
        prop = by_name[tool]["parameters"]["properties"]
        for key in path:
            prop = prop[key]
        assert "enum" not in prop, f"{tool}.{'.'.join(path)} still constrains values"

    filter_by = by_name["bulk_update"]["parameters"]["properties"]["filter_by"]
    assert "enum" not in filter_by["properties"]["module"]

    # No SAP module code should survive anywhere in the serialized schemas.
    serialized = _json.dumps(TOOLS)
    for code in ("TRM", "HCM", "WRICEF"):
        assert code not in serialized


# 12. Regression for TDD §16.23 (Phase 6): init_db() failures are swallowed at boot
# (main.py:lifespan) so the process keeps serving for debuggability — but a live SELECT 1
# alone can't distinguish "Postgres is down" from "Postgres is up with no tables ever
# created". app.state.db_initialized closes that gap.
@pytest.mark.asyncio
async def test_readiness_check_reports_unready_when_db_schema_never_initialized():
    from fastapi import HTTPException
    from app.api.health import readiness_check

    mock_session = MagicMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_session.execute = AsyncMock(return_value=None)

    class FakeState:
        db_initialized = False

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    with patch("app.api.health.AsyncSessionLocal", return_value=mock_session), \
         patch("app.queue.producer.redis_client.ping", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc_info:
            await readiness_check(request=FakeRequest())

    assert exc_info.value.status_code == 503
    assert "db_schema" in exc_info.value.detail
    # Postgres and Redis are themselves healthy — only the schema-init flag fails, which
    # is exactly the case a live SELECT 1 alone can't detect.
    assert exc_info.value.detail["postgres"] == "ok"
    assert exc_info.value.detail["redis"] == "ok"


@pytest.mark.asyncio
async def test_readiness_check_passes_when_db_initialized():
    from app.api.health import readiness_check

    mock_session = MagicMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_session.execute = AsyncMock(return_value=None)

    class FakeState:
        db_initialized = True

    class FakeApp:
        state = FakeState()

    class FakeRequest:
        app = FakeApp()

    with patch("app.api.health.AsyncSessionLocal", return_value=mock_session), \
         patch("app.queue.producer.redis_client.ping", AsyncMock(return_value=True)):
        result = await readiness_check(request=FakeRequest())

    assert result["status"] == "ready"


# 13. Regression for TDD §7.3 / §16.3 (Phase 6): build_column_map (a complete two-pass
# LLM alias generator) was defined but never called from anywhere, so every deployment
# ran on the static COLUMN_ALIASES fallback — hardcoded to one customer's real sheet
# headers, and the root cause of the whitespace-column class of bugs (§16.2) for anyone
# else's tracker. detect_all_tabs must now generate and attach a per-tab column_map.
@pytest.mark.asyncio
async def test_detect_all_tabs_wires_in_build_column_map():
    from app.core.schema_detect import detect_all_tabs

    mock_service = MagicMock()
    mock_service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "SD"}}]
    }
    mock_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "values": [["RICEFW ID", "Module", "Dev Status"]]
    }

    fake_tab_schema = {
        "is_tracker_sheet": True,
        "header_row_index": 0,
        "primary_id_column": "RICEFW ID",
    }
    fake_column_map = {"RICEFW ID": ["id", "ricefw"]}

    with patch("app.core.schema_detect.detect_schema_config", AsyncMock(return_value=fake_tab_schema)), \
         patch("app.core.schema_detect.build_column_map", AsyncMock(return_value=fake_column_map)) as mock_build_map:
        result = await detect_all_tabs(mock_service, "sheet-123", client=AsyncMock())

    assert mock_build_map.called
    assert mock_build_map.call_args.args[0] == ["RICEFW ID", "Module", "Dev Status"]
    assert result["tabs"]["SD"]["column_map"] == fake_column_map


# 14. Field-level RBAC previously covered only update_cell and bulk_update — add_row's
# free-form `fields` dict and format_row (which always writes the Color column) were
# ungated, so a user restricted to a specific column could still write anywhere via
# either tool.
def test_field_restricted_editor_add_row_extra_fields_gated():
    field_editor = PermissionChecker("editor@example.com", role="editor", allowed_fields=["Dev Status"], denied_operations=[])

    # module/type/description/assigned_to are structural, not gated by allowed_fields.
    allowed, reason = field_editor.can_execute("add_row", {
        "module": "SD", "type": "R", "description": "New object", "assigned_to": "dev@example.com"
    })
    assert allowed is True

    # An allowed extra column passes.
    allowed, reason = field_editor.can_execute("add_row", {
        "module": "SD", "type": "R", "description": "New object",
        "fields": {"Dev Status": "Ready for Dev"}
    })
    assert allowed is True

    # A disallowed extra column is the escape hatch this closes.
    allowed, reason = field_editor.can_execute("add_row", {
        "module": "SD", "type": "R", "description": "New object",
        "fields": {"Business Owner": "Alex"}
    })
    assert allowed is False
    assert "don't have write access to: **Business Owner**" in reason


def test_field_restricted_editor_format_row_gated_on_color():
    field_editor = PermissionChecker("editor@example.com", role="editor", allowed_fields=["Dev Status"], denied_operations=[])
    allowed, reason = field_editor.can_execute("format_row", {"ricefw_id": "SD-012", "color": "green", "scope": "entire_row"})
    assert allowed is False
    assert "don't have write access to **Color**" in reason

    color_editor = PermissionChecker("editor@example.com", role="editor", allowed_fields=["Color"], denied_operations=[])
    allowed, reason = color_editor.can_execute("format_row", {"ricefw_id": "SD-012", "color": "green", "scope": "color_column_only"})
    assert allowed is True


def test_unrestricted_editor_still_allowed_add_row_and_format_row():
    """allowed_fields == ["*"] (the default) must remain fully permissive — this fix
    should only add friction for accounts an admin has deliberately restricted."""
    editor = PermissionChecker("editor@example.com", role="editor", allowed_fields=["*"], denied_operations=[])

    allowed, _ = editor.can_execute("add_row", {
        "module": "SD", "type": "R", "description": "New object",
        "fields": {"Business Owner": "Alex"}
    })
    assert allowed is True

    allowed, _ = editor.can_execute("format_row", {"ricefw_id": "SD-012", "color": "red", "scope": "entire_row"})
    assert allowed is True


# 20. Regression for the 2026-08 incident: a Redis outage must be classified as
# infrastructure, not as a bad argument. The model previously received the same
# "formulate a corrected tool call" note for every failure, so it rewrote calls
# that could never succeed and told the user their edit had been rejected.
def test_classify_redis_readonly_as_infrastructure():
    import redis.exceptions as redis_exc

    exc = redis_exc.ReadOnlyError("You can't write against a read only replica.")
    assert classify_error(exc) == INFRASTRUCTURE

    result = error_result(exc, "update_cell")
    assert result["ok"] is False
    assert result["error_kind"] == INFRASTRUCTURE
    # The raw text is preserved for logs/audit...
    assert "read only replica" in result["error"]
    # ...but what the user is shown never mentions replicas.
    assert "replica" not in result["user_message"].lower()
    assert "not saved" in result["user_message"].lower()


# 21. Guidance must forbid a retry for failures a retry cannot fix, and invite one
# only where corrected arguments could plausibly succeed.
def test_failure_note_guidance_differs_by_kind():
    infra = failure_note("update_cell", error_result(ConnectionError("redis down"), "update_cell"))
    assert "infrastructure" in infra
    assert "Do NOT retry" in infra

    bad_args = failure_note("search_rows", error_result(KeyError("Dev Status"), "search_rows"))
    assert "invalid_request" in bad_args
    assert "retry once with corrected arguments" in bad_args


# 21b. A duplicated ID is not a bad argument. No rewrite of the call can fix it, so the
# model must be told to stop and ask rather than spend iterations rephrasing (§16.7).
def test_ambiguous_id_is_classified_and_forbids_a_retry():
    assert classify_error(AmbiguousRowError("SD-002", [4, 6])) == AMBIGUOUS_ID

    result = ambiguous_id_result("SD-002", [4, 6], "update_cell")
    assert result["ok"] is False
    assert result["error_kind"] == AMBIGUOUS_ID
    assert result["ambiguous_rows"] == [4, 6]

    note = failure_note("update_cell", result)
    assert "ambiguous_id" in note
    assert "Do NOT retry" in note
    # The alternative offered must be asking the user, not picking a row.
    assert "guess" in note


def test_ambiguous_id_user_message_names_the_rows():
    """Row numbers are the actionable part — "it's duplicated" alone leaves the user
    nowhere to go, so this kind carries its detail through rather than replacing it."""
    message = ambiguous_id_result("SD-002", [4, 6])["user_message"]
    assert "4, 6" in message
    assert "SD-002" in message


# 22. Google HTTP statuses map to distinct kinds — 429 must not read as a
# permission problem, and 403 must not read as something to retry.
@pytest.mark.parametrize("status,expected", [
    (401, AUTH),
    (404, NOT_FOUND),
    (429, RATE_LIMIT),
    (503, UPSTREAM),
])
def test_classify_google_http_errors(status, expected):
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = "err"
    assert classify_error(HttpError(resp=resp, content=b"{}")) == expected
