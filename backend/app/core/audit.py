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


# Convenience wrappers to map legacy audit helper calls to the new system
def log_update_cell(
    user_email: str,
    session_id: Optional[UUID],
    spreadsheet_id: str,
    sheet_tab: str,
    ricefw_id: str,
    field: str,
    old_value: str,
    new_value: str,
    result: dict,
) -> None:
    log_audit(
        user_email=user_email,
        session_id=session_id,
        tool_name="update_cell",
        spreadsheet_id=spreadsheet_id,
        sheet_tab=sheet_tab,
        ricefw_id=ricefw_id,
        field=field,
        old_value=old_value,
        new_value=new_value,
        args={"ricefw_id": ricefw_id, "field": field, "value": new_value},
        result_ok=result.get("ok", False),
        error=result.get("error", ""),
    )


def log_bulk_update(
    user_email: str,
    session_id: Optional[UUID],
    spreadsheet_id: str,
    sheet_tab: str,
    args: dict,
    result: dict,
) -> None:
    succeeded = result.get("succeeded", [])
    failed = result.get("failed", [])

    for rid in succeeded:
        log_audit(
            user_email=user_email,
            session_id=session_id,
            tool_name="bulk_update",
            spreadsheet_id=spreadsheet_id,
            sheet_tab=sheet_tab,
            ricefw_id=rid,
            field=args.get("set_field", ""),
            new_value=args.get("set_value", ""),
            args=args,
            result_ok=True,
        )
    for f in failed:
        log_audit(
            user_email=user_email,
            session_id=session_id,
            tool_name="bulk_update",
            spreadsheet_id=spreadsheet_id,
            sheet_tab=sheet_tab,
            ricefw_id=f.get("id", ""),
            field=args.get("set_field", ""),
            new_value=args.get("set_value", ""),
            args=args,
            result_ok=False,
            error=f.get("error", ""),
        )


def log_format_row(
    user_email: str,
    session_id: Optional[UUID],
    spreadsheet_id: str,
    sheet_tab: str,
    args: dict,
    result: dict,
) -> None:
    log_audit(
        user_email=user_email,
        session_id=session_id,
        tool_name="format_row",
        spreadsheet_id=spreadsheet_id,
        sheet_tab=sheet_tab,
        ricefw_id=args.get("ricefw_id", ""),
        field="Color",
        new_value=f"{args.get('color','') or 'none'} / {args.get('scope','') or 'none'}",
        args=args,
        result_ok=result.get("ok", False),
        error=result.get("error", ""),
    )


def log_add_row(
    user_email: str,
    session_id: Optional[UUID],
    spreadsheet_id: str,
    sheet_tab: str,
    ricefw_id: str,
    args: dict,
    result: dict,
) -> None:
    log_audit(
        user_email=user_email,
        session_id=session_id,
        tool_name="add_row",
        spreadsheet_id=spreadsheet_id,
        sheet_tab=sheet_tab,
        ricefw_id=ricefw_id,
        args=args,
        result_ok=result.get("ok", False),
        error=result.get("error", ""),
    )
