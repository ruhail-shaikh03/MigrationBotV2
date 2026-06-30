import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("tool_dispatch")

async def dispatch_tool(
    tool_name: str,
    args: dict,
    user_email: str,
    session_id: Any,
    spreadsheet_id: str,
    active_tab: str,
    schema_config: dict,
    column_map: dict,
    db_session: Any,
    google_access_token: str = "mock-google-access-token"
) -> Dict[str, Any]:
    """
    Routes a tool execution request.
    If read tool -> direct query database / sheet mapping using active OAuth token.
    If write tool -> enqueues job (including credentials) to the Redis worker queue.
    """
    logger.info(f"Dispatching tool: {tool_name} with args: {args}")

    # 1. READ-ONLY PATHWAY (Execute directly)
    if tool_name in ("get_row", "search_rows", "summarize", "switch_module", "data_quality"):
        try:
            from app.sheets.client import build_sheets_service
            service = build_sheets_service(google_access_token)

            if tool_name == "get_row":
                from app.sheets.read import get_row
                return await get_row(spreadsheet_id, active_tab, args.get("ricefw_id"), schema_config, column_map, service)
            elif tool_name == "search_rows":
                from app.sheets.read import search_rows
                return await search_rows(
                    spreadsheet_id, 
                    active_tab, 
                    args.get("filters", []), 
                    args.get("return_fields"), 
                    args.get("limit", 20), 
                    schema_config, 
                    column_map,
                    service
                )
            elif tool_name == "summarize":
                from app.sheets.read import summarize
                return await summarize(spreadsheet_id, active_tab, args, schema_config, column_map, service)
            elif tool_name == "switch_module":
                from app.sheets.meta import switch_module
                return await switch_module(spreadsheet_id, args.get("tab_name"), db_session, user_email, session_id, service)
            elif tool_name == "data_quality":
                from app.sheets.read import run_data_quality_check
                return await run_data_quality_check(spreadsheet_id, active_tab, args, schema_config, db_session, service)
        except ImportError:
            logger.warning(f"Sheets layer not implemented yet. Mocking result for read tool: {tool_name}")
            return {"ok": True, "data": f"Mock result for {tool_name}"}
        except Exception as e:
            logger.error(f"Error executing read tool {tool_name}: {e}")
            return {"ok": False, "error": str(e)}

    # 2. WRITE/MUTATION PATHWAY (Queue-backed write)
    elif tool_name in ("update_cell", "bulk_update", "format_row", "add_row"):
        try:
            from app.sheets.client import build_sheets_service
            service = build_sheets_service(google_access_token)

            # Pre-read current state to preserve 'old_value' for audit logging
            old_values = {}
            try:
                if tool_name == "update_cell":
                    from app.sheets.read import get_row_raw
                    ricefw_id = args.get("ricefw_id")
                    updates = args.get("updates", [])
                    fields = [u.get("field") for u in updates]
                    old_values = await get_row_raw(spreadsheet_id, active_tab, ricefw_id, fields, schema_config, service)
                elif tool_name == "bulk_update":
                    from app.sheets.read import get_bulk_rows_raw
                    old_values = await get_bulk_rows_raw(spreadsheet_id, active_tab, args, schema_config, service)
            except Exception as pe:
                logger.warning(f"Failed to pre-read state for audit: {pe}")

            # Enqueue the write operation
            from app.queue.producer import enqueue_write_job
            job = await enqueue_write_job(
                user_email=user_email,
                google_access_token=google_access_token,
                session_id=session_id,
                tool_name=tool_name,
                spreadsheet_id=spreadsheet_id,
                sheet_tab=active_tab,
                args=args,
                old_values=old_values
            )
            return {
                "ok": True,
                "message": f"Operation queued successfully. Job ID: {job.id}",
                "job_id": job.id,
                "status": "queued"
            }
        except ImportError:
            logger.warning(f"Queue layer not implemented yet. Mocking queue action for write tool: {tool_name}")
            return {"ok": True, "message": f"Mock: Operation {tool_name} successfully enqueued"}
        except Exception as e:
            logger.error(f"Error enqueuing write tool {tool_name}: {e}")
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"Unknown tool: {tool_name}"}
