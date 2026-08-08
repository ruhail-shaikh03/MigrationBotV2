import json
import asyncio
import logging
from datetime import datetime, timezone
import redis
import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.models.project import Project
from app.queue.schemas import WriteJobPayload, JOB_STATE_PREFIX, JOB_STATE_TTL_SECONDS
from app.queue.events import publish_queue_update
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

QUEUE_KEY = "migrationbot:write_queue"
# BLMOVE atomically relocates a job here on pickup, so a worker that dies between
# pickup and the LREM at the end of a clean pass leaves the job here, not gone —
# recover_stale_jobs() re-queues (or dead-letters) anything still here on the next
# worker startup, which is also what happens automatically after a Docker restart
# (docker-compose.yml: worker has restart: unless-stopped).
PROCESSING_KEY = "migrationbot:write_queue:processing"
DEAD_LETTER_KEY = "migrationbot:write_queue:dead_letter"
MAX_ATTEMPTS = 3


async def _set_job_state(redis_client, job_id: str, **fields) -> None:
    """Merge fields into the job's state hash so the job_id already returned to the
    caller (tool_dispatch.py:dispatch_tool) stays queryable after the fact, not just
    while the write is in flight."""
    if not job_id:
        return
    key = f"{JOB_STATE_PREFIX}:{job_id}"
    try:
        existing = await redis_client.get(key)
        state = json.loads(existing) if existing else {}
        state.update(fields)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        await redis_client.set(key, json.dumps(state, ensure_ascii=False), ex=JOB_STATE_TTL_SECONDS)
    except Exception as e:
        # Job-state tracking is observability, not correctness — never let it block
        # or fail the write it's describing.
        logger.warning(f"Failed to update job state for {job_id}: {e}")


async def recover_stale_jobs(redis_client) -> int:
    """Run once at worker startup. Anything still in PROCESSING_KEY was picked up by
    a previous run that never reached a clean completion (crash, OOM kill, forced
    container restart mid-job) — BLMOVE puts it there atomically on pickup, and only
    a clean pass removes it. Re-queues under MAX_ATTEMPTS; dead-letters and notifies
    the user past that, rather than retrying forever or silently losing the write."""
    recovered = 0
    while True:
        raw = await redis_client.lpop(PROCESSING_KEY)
        if raw is None:
            break

        try:
            envelope = json.loads(raw)
        except Exception:
            logger.error(f"Dropping unparseable stale job during recovery: {raw!r}")
            continue

        job_id = envelope.get("job_id")
        payload = envelope.get("payload", {}) or {}
        attempt = envelope.get("attempt", 0) + 1
        envelope["attempt"] = attempt
        recovered += 1

        if attempt > MAX_ATTEMPTS:
            logger.error(f"Job {job_id} exceeded {MAX_ATTEMPTS} attempts after worker crash(es); dead-lettering.")
            await redis_client.rpush(DEAD_LETTER_KEY, json.dumps(envelope, ensure_ascii=False))
            await _set_job_state(redis_client, job_id, status="dead_letter", attempt=attempt)
            try:
                await publish_queue_update(
                    user_email=payload.get("user_email", ""),
                    job_id=job_id,
                    status="failed",
                    tool_name=payload.get("tool_name", ""),
                    args=payload.get("args", {}),
                    session_id=payload.get("session_id"),
                    error=f"Write did not complete after {MAX_ATTEMPTS} attempts (worker restarted mid-job each time)."
                )
            except Exception:
                pass
        else:
            logger.warning(f"Recovering stale job {job_id} (attempt {attempt}/{MAX_ATTEMPTS}) left by a crashed worker.")
            await redis_client.rpush(QUEUE_KEY, json.dumps(envelope, ensure_ascii=False))
            await _set_job_state(redis_client, job_id, status="queued", attempt=attempt)

    return recovered


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
    service = build_sheets_service(payload.google_access_token, payload.google_refresh_token)

    # 3. Dispatch and execute mutation
    tool = payload.tool_name
    args = payload.args
    old_values = payload.old_values

    result_ok = False
    error_msg = None

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
            type_val = args.get("type", "")
            desc = args.get("description", "")
            assignee = args.get("assigned_to", "")
            fields = args.get("fields", {})

            # The add_row tool schema (tool_schemas.py) declares no ricefw_id/prefix
            # property, so the model can never supply either — the ID is always
            # computed here, sequentially, right before the write. That's deliberate:
            # doing it server-side avoids the race a client-computed ID would hit
            # under concurrent adds.
            tab_schema = schema_config.get("tabs", {}).get(payload.sheet_tab, {}) if "tabs" in schema_config else schema_config
            data_start_row = tab_schema.get("data_start_row", 3)
            primary_id_pos = tab_schema.get("primary_id_position", "B")
            ricefw_id = await next_ricefw_id(
                service=service,
                spreadsheet_id=payload.spreadsheet_id,
                sheet_name=payload.sheet_tab,
                module=module,
                prefix=None,
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
            error_msg = f"Unsupported queue tool: {tool}"
            logger.error(error_msg)

    except Exception as e:
        logger.error(f"Failed to execute mutation {tool} on sheet: {e}")
        result_ok = False
        error_msg = str(e)
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

    # 4. Notify the originating client of the terminal job state over its WebSocket
    await publish_queue_update(
        user_email=payload.user_email,
        job_id=job_id,
        status="completed" if result_ok else "failed",
        tool_name=tool,
        args=args,
        session_id=payload.session_id,
        error=error_msg
    )


async def start_worker():
    """Loops indefinitely, consuming write job entries from Redis list queue."""
    logger.info(f"Connecting to Redis at {settings.REDIS_URL}...")
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

    recovered = await recover_stale_jobs(redis_client)
    if recovered:
        logger.warning(f"Recovered {recovered} job(s) left behind by a previous worker crash.")

    logger.info("Worker is running and listening for queue updates...")

    try:
        while True:
            # BLMOVE blocks like BLPOP but atomically relocates the job into
            # PROCESSING_KEY on pickup instead of just removing it — see
            # recover_stale_jobs()'s docstring for why that matters.
            try:
                raw_data = await redis_client.blmove(QUEUE_KEY, PROCESSING_KEY, timeout=10, src="LEFT", dest="RIGHT")
                if raw_data is None:
                    continue
            except (TimeoutError, redis.exceptions.TimeoutError):
                continue

            job_id = None
            try:
                envelope = json.loads(raw_data)
                job_id = envelope.get("job_id")
                payload_dict = envelope.get("payload")

                await _set_job_state(redis_client, job_id, status="processing")
                # process_job has its own try/except around the actual mutation, so
                # reaching the line below means a terminal outcome was recorded
                # (audit row + queue_update), success or a real business failure —
                # either way the job is done, not crashed.
                await process_job(job_id, payload_dict)
                await _set_job_state(redis_client, job_id, status="done")
            except Exception as e:
                logger.error(f"Error parsing or processing raw queue message: {e}")
                if job_id:
                    await _set_job_state(redis_client, job_id, status="error", error=str(e))
                # Left in PROCESSING_KEY deliberately: this except firing means
                # something outside process_job's own error handling went wrong
                # (envelope/payload malformed, or a bug), so we can't be sure what
                # state the write is in. recover_stale_jobs() retries it (bounded)
                # on the next worker start rather than silently dropping it here.
            else:
                await redis_client.lrem(PROCESSING_KEY, 1, raw_data)

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
