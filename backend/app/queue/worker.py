import json
import asyncio
import logging
from typing import Dict, Any, Optional
import redis
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.models.project import Project
from app.queue.schemas import WriteJobPayload
from app.sheets.client import build_sheets_service
from app.sheets.write import update_cell, bulk_update, add_row
from app.sheets.format import format_row
from app.core.audit import _write_audit_record

# Configure logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("queue_worker")


async def process_job(job_id: str, payload_dict: dict) -> None:
    """Processes a single enqueued write job, executing sheets API mutations and logging audit trails."""
    payload = WriteJobPayload(**payload_dict)
    logger.info(f"Processing job {job_id} ({payload.tool_name}) for user {payload.user_email}.")

    # 1. Fetch latest project configuration (schema_config) from PostgreSQL
    async with AsyncSessionLocal() as db:
        proj_res = await db.execute(select(Project).where(Project.spreadsheet_id == payload.spreadsheet_id))
        project = proj_res.scalar()
        schema_config = project.schema_config if project else {}

    # 2. Build Google Sheets API client with active credentials
    # Supports mock credential fallback for test suites
    service = build_sheets_service(payload.google_access_token)

    # 3. Dispatch and execute mutation
    tool = payload.tool_name
    args = payload.args
    old_values = payload.old_values

    result_ok = False
    error_msg = None
    result_data = {}

    try:
        if tool == "update_cell":
            # updates parameter: List[dict] with 'field' and 'value'
            updates = args.get("updates", [])
            ricefw_id = args.get("ricefw_id", "")
            
            res = await update_cell(
                service=service,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_tab=payload.sheet_tab,
                ricefw_id=ricefw_id,
                updates=updates,
                schema_config=schema_config
            )
            result_ok = res.get("ok", False)
            error_msg = res.get("error")
            result_data = res

            # Log audit records for each updated field
            for item in updates:
                field = item.get("field", "")
                val = item.get("value", "")
                old_val = old_values.get(field, "")
                await _write_audit_record(
                    user_email=payload.user_email,
                    session_id=payload.session_id,
                    tool_name=tool,
                    spreadsheet_id=payload.spreadsheet_id,
                    sheet_tab=payload.sheet_tab,
                    ricefw_id=ricefw_id,
                    field=field,
                    old_value=old_val,
                    new_value=val,
                    args=args,
                    result_ok=result_ok,
                    error=error_msg
                )

        elif tool == "bulk_update":
            res = await bulk_update(
                service=service,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_tab=payload.sheet_tab,
                args=args,
                schema_config=schema_config
            )
            result_ok = res.get("ok", False)
            error_msg = res.get("error")
            result_data = res

            set_field = args.get("set_field", "")
            set_value = args.get("set_value", "")

            # Log audits for succeeded records
            for rid in res.get("succeeded", []):
                old_val = old_values.get(rid, {}).get(set_field, "")
                await _write_audit_record(
                    user_email=payload.user_email,
                    session_id=payload.session_id,
                    tool_name=tool,
                    spreadsheet_id=payload.spreadsheet_id,
                    sheet_tab=payload.sheet_tab,
                    ricefw_id=rid,
                    field=set_field,
                    old_value=old_val,
                    new_value=set_value,
                    args=args,
                    result_ok=True,
                    error=None
                )

            # Log audits for failed records
            for f in res.get("failed", []):
                rid = f.get("id", "")
                old_val = old_values.get(rid, {}).get(set_field, "")
                await _write_audit_record(
                    user_email=payload.user_email,
                    session_id=payload.session_id,
                    tool_name=tool,
                    spreadsheet_id=payload.spreadsheet_id,
                    sheet_tab=payload.sheet_tab,
                    ricefw_id=rid,
                    field=set_field,
                    old_value=old_val,
                    new_value=set_value,
                    args=args,
                    result_ok=False,
                    error=f.get("error", "Write failed")
                )

        elif tool == "format_row":
            ricefw_id = args.get("ricefw_id", "")
            color = args.get("color", "")
            scope = args.get("scope", "color_column_only")
            
            res = await format_row(
                service=service,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_tab=payload.sheet_tab,
                ricefw_id=ricefw_id,
                color=color,
                scope=scope,
                schema_config=schema_config
            )
            result_ok = res.get("ok", False)
            error_msg = res.get("error")
            result_data = res

            await _write_audit_record(
                user_email=payload.user_email,
                session_id=payload.session_id,
                tool_name=tool,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_tab=payload.sheet_tab,
                ricefw_id=ricefw_id,
                field="Color",
                old_value="",
                new_value=f"{color} / {scope}",
                args=args,
                result_ok=result_ok,
                error=error_msg
            )

        elif tool == "add_row":
            from app.sheets.meta import next_ricefw_id
            module = args.get("module", "")
            prefix = args.get("prefix")
            type_val = args.get("type", "")
            desc = args.get("description", "")
            assignee = args.get("assigned_to", "")
            fields = args.get("fields", {})

            # 1. Dynamically compute the next RICEFW ID sequentially
            tab_schema = schema_config.get("tabs", {}).get(payload.sheet_tab, {}) if "tabs" in schema_config else schema_config
            data_start_row = tab_schema.get("data_start_row", 3)
            primary_id_pos = tab_schema.get("primary_id_position", "B")
            # In add_row, we auto-assign ID if not supplied
            ricefw_id = args.get("ricefw_id")
            if not ricefw_id:
                ricefw_id = await next_ricefw_id(
                    service=service,
                    spreadsheet_id=payload.spreadsheet_id,
                    sheet_name=payload.sheet_tab,
                    module=module,
                    prefix=prefix,
                    data_start_row=data_start_row,
                    primary_id_pos=primary_id_pos
                )

            res = await add_row(
                service=service,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_tab=payload.sheet_tab,
                ricefw_id=ricefw_id,
                module=module,
                type=type_val,
                description=desc,
                assigned_to=assignee,
                fields=fields,
                schema_config=schema_config
            )
            result_ok = res.get("ok", False)
            error_msg = res.get("error")
            result_data = res

            await _write_audit_record(
                user_email=payload.user_email,
                session_id=payload.session_id,
                tool_name=tool,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_tab=payload.sheet_tab,
                ricefw_id=ricefw_id,
                field="ID",
                old_value="",
                new_value=ricefw_id,
                args=args,
                result_ok=result_ok,
                error=error_msg
            )
        else:
            logger.error(f"Unsupported queue tool: {tool}")
            
    except Exception as e:
        logger.error(f"Failed to execute mutation {tool} on sheet: {e}")
        await _write_audit_record(
            user_email=payload.user_email,
            session_id=payload.session_id,
            tool_name=tool,
            spreadsheet_id=payload.spreadsheet_id,
            sheet_tab=payload.sheet_tab,
            ricefw_id=args.get("ricefw_id", "ERROR"),
            field="Mutation",
            old_value="",
            new_value="",
            args=args,
            result_ok=False,
            error=str(e)
        )


async def start_worker():
    """Loops indefinitely, consuming write job entries from Redis list queue."""
    logger.info(f"Connecting to Redis at {settings.REDIS_URL}...")
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    queue_key = "migrationbot:write_queue"
    
    logger.info("Worker is running and listening for queue updates...")
    
    try:
        while True:
            # BLPOP blocks asynchronously until a job is pushed into the queue list
            try:
                pop_res = await redis_client.blpop(queue_key, timeout=10)
                if not pop_res:
                    continue
            except (TimeoutError, redis.exceptions.TimeoutError):
                continue

            _, raw_data = pop_res
            try:
                envelope = json.loads(raw_data)
                job_id = envelope.get("job_id")
                payload_dict = envelope.get("payload")
                
                # Execute the spreadsheet update
                await process_job(job_id, payload_dict)
                
            except Exception as e:
                logger.error(f"Error parsing or processing raw queue message: {e}")
            
            # Enforce 1-second interval rate limiting throttle to protect Google API quotas
            await asyncio.sleep(1.0)
            
    except asyncio.CancelledError:
        logger.info("Worker cancel signal received. Stopping worker loop.")
    finally:
        await redis_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(start_worker())
    except KeyboardInterrupt:
        logger.info("Worker stopped manually.")
