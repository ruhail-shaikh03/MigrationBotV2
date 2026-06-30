import asyncio
import logging
from uuid import UUID
from typing import Optional, Dict, Any
from app.db.engine import AsyncSessionLocal
from app.models.audit_log import AuditLog

logger = logging.getLogger("audit")

async def _write_audit_record(
    user_email: str,
    session_id: Optional[UUID],
    tool_name: str,
    spreadsheet_id: Optional[str],
    sheet_tab: Optional[str],
    ricefw_id: Optional[str],
    field: Optional[str],
    old_value: Optional[str],
    new_value: Optional[str],
    args: Optional[Dict[str, Any]],
    result_ok: bool,
    error: Optional[str]
) -> None:
    """
    Worker function executed in the background. Connects to the database and commits
    the audit row. Suppresses and logs any errors.
    """
    try:
        async with AsyncSessionLocal() as db:
            log_record = AuditLog(
                user_email=user_email,
                session_id=session_id,
                tool_name=tool_name,
                spreadsheet_id=spreadsheet_id,
                sheet_tab=sheet_tab,
                ricefw_id=ricefw_id,
                field=field,
                old_value=old_value,
                new_value=new_value,
                args_json=args,
                result_ok=result_ok,
                error=error
            )
            db.add(log_record)
            await db.commit()
    except Exception as e:
        logger.error(f"[AUDIT WRITE FAILED] {e}")


def log_audit(
    user_email: str,
    session_id: Optional[UUID],
    tool_name: str,
    spreadsheet_id: Optional[str],
    sheet_tab: Optional[str],
    ricefw_id: Optional[str] = "",
    field: Optional[str] = "",
    old_value: Optional[str] = "",
    new_value: Optional[str] = "",
    args: Optional[Dict[str, Any]] = None,
    result_ok: bool = True,
    error: Optional[str] = "",
) -> None:
    """
    Schedule a non-blocking background task to write an audit row.
    """
    asyncio.create_task(
        _write_audit_record(
            user_email=user_email,
            session_id=session_id,
            tool_name=tool_name,
            spreadsheet_id=spreadsheet_id,
            sheet_tab=sheet_tab,
            ricefw_id=ricefw_id,
            field=field,
            old_value=old_value,
            new_value=new_value,
            args=args,
            result_ok=result_ok,
            error=error
        )
    )
