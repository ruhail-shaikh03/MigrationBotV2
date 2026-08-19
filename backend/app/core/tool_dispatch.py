import logging
from typing import Dict, Any, Optional

from app.core.errors import ambiguous_id_result, error_result, INVALID_REQUEST, USER_MESSAGE

logger = logging.getLogger("tool_dispatch")


async def _resolver_for_spreadsheet(db_session: Any, spreadsheet_id: str):
    """The alias map for whichever project owns this spreadsheet.

    The agent path carries a spreadsheet id rather than a project id, so the project is
    looked up on the way. Best-effort: an unreachable database degrades to unmerged names
    rather than failing the read, matching `dashboard.py:_resolver_for`.
    """
    from app.core.aliases import PersonResolver

    try:
        from sqlalchemy import select
        from app.models.person_alias import PersonAlias
        from app.models.project import Project

        project = (await db_session.execute(
            select(Project).where(Project.spreadsheet_id == spreadsheet_id)
        )).scalar()
        if not project:
            return PersonResolver()
        rows = (await db_session.execute(
            select(PersonAlias).where(PersonAlias.project_id == project.id)
        )).scalars().all()
        return PersonResolver.from_rows(rows)
    except Exception as e:
        logger.warning(f"Alias map unavailable for {spreadsheet_id}: {e}")
        return PersonResolver()

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
    google_access_token: str = "mock-google-access-token",
    google_refresh_token: Optional[str] = None
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
            service = build_sheets_service(google_access_token, google_refresh_token)

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
                # count_by_field on a people column must split shared cells and apply the
                # alias map, or chat reports "Minhaj Alam & Dawood" as a person while the
                # dashboard beside it reports two, from the same sheet and the same tab.
                resolver = await _resolver_for_spreadsheet(db_session, spreadsheet_id)
                return await summarize(
                    spreadsheet_id, active_tab, args, schema_config, column_map, service,
                    resolver=resolver,
                )
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
            # Classified rather than str(e): a Sheets 429 and a mistyped column
            # name are both "an error" here, but only one is worth retrying.
            return error_result(e, tool_name)

    # 2. WRITE/MUTATION PATHWAY (Queue-backed write)
    elif tool_name in ("update_cell", "bulk_update", "format_row", "add_row"):
        try:
            from app.sheets.client import build_sheets_service
            service = build_sheets_service(google_access_token, google_refresh_token)

            # Refuse an ambiguous single-row target before it reaches the queue. The
            # worker re-checks against a live scan and is what actually guarantees
            # correctness; this exists so the user is told at dispatch rather than
            # waiting for a queue_update failure. Answered from cache, so normally free.
            if tool_name in ("update_cell", "format_row") and args.get("row_number") is None:
                target_id = args.get("ricefw_id")
                if target_id:
                    try:
                        from app.sheets.read import cached_id_row_nums
                        matches = await cached_id_row_nums(
                            spreadsheet_id, active_tab, target_id, schema_config, service
                        )
                    except Exception as ce:
                        # A cache that cannot answer must not block a write the worker
                        # would have accepted — same rule as every other cache tier.
                        logger.warning(f"Ambiguity pre-check unavailable for {target_id}: {ce}")
                        matches = []
                    if len(matches) > 1:
                        return ambiguous_id_result(target_id, matches, tool_name)

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
                    old_values = await get_bulk_rows_raw(spreadsheet_id, active_tab, args, schema_config, service, column_map)
            except Exception as pe:
                logger.warning(f"Failed to pre-read state for audit: {pe}")

            # Enqueue the write operation
            from app.queue.producer import enqueue_write_job
            job = await enqueue_write_job(
                user_email=user_email,
                google_access_token=google_access_token,
                google_refresh_token=google_refresh_token,
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
            # Reaching here means the job never made it onto the queue, so nothing
            # will retry it later — the classification is what stops the model
            # from reporting a queue outage as a rejected edit.
            return error_result(e, tool_name)

    return {
        "ok": False,
        "error": f"Unknown tool: {tool_name}",
        "error_kind": INVALID_REQUEST,
        "user_message": USER_MESSAGE[INVALID_REQUEST],
    }
