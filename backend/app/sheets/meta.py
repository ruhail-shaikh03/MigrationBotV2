import logging
from typing import List, Dict, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.session import Session as UserSession
from app.sheets.retry import _with_retry

logger = logging.getLogger("sheets_meta")

async def get_sheet_id(service: Any, spreadsheet_id: str, sheet_name: str) -> int:
    """Get the internal sheetId integer for formatting operations."""
    meta = await _with_retry(lambda: service.spreadsheets().get(
        spreadsheetId=spreadsheet_id
    ).execute())
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == sheet_name:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Sheet tab '{sheet_name}' not found.")


async def get_header_row(service: Any, spreadsheet_id: str, sheet_name: str, header_row_num: int) -> List[str]:
    """Fetch the exact header row cell values."""
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{header_row_num}:{header_row_num}"
    ).execute())
    values = result.get("values", [[]])
    return [str(cell).strip() for cell in values[0]] if values else []


async def get_all_ids(service: Any, spreadsheet_id: str, sheet_name: str, data_start_row: int, primary_id_pos: str = "B") -> List[str]:
    """Read the unique ID column starting from the data start row."""
    col = primary_id_pos or "B"
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{col}{data_start_row}:{col}"
    ).execute())
    return [str(r[0]).strip() for r in result.get("values", []) if r and str(r[0]).strip()]


async def get_id_row_map(service: Any, spreadsheet_id: str, sheet_name: str, data_start_row: int, primary_id_pos: str = "B") -> Dict[str, List[int]]:
    """Scans the ID column once and returns {RICEFW_ID (stripped, uppercased): [row_num, ...]}.
    find_row_num does the same scan but for a single ID and throws the result away —
    called once per target ID from a bulk op, that was ~N full-column scans for N
    IDs. Callers resolving multiple IDs in the same operation (bulk_update,
    get_bulk_rows_raw) should scan once here and look up row numbers in memory.

    Every match is kept, not just the last. This used to be `id_map[key] = row_num`,
    which meant a duplicated ID silently resolved to its *last* occurrence — while
    `find_row_num` resolved the same ID to its *first*. `update_cell` and `bulk_update`
    could therefore write to two different physical rows for one ID (§16.7). Callers
    must decide what a multi-row match means; none of them may quietly pick one."""
    col = primary_id_pos or "B"
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{col}{data_start_row}:{col}"
    ).execute())
    id_map: Dict[str, List[int]] = {}
    for i, row in enumerate(result.get("values", [])):
        if row and str(row[0]).strip():
            id_map.setdefault(str(row[0]).strip().upper(), []).append(data_start_row + i)
    return id_map


async def detect_prefix(service: Any, spreadsheet_id: str, sheet_name: str, data_start_row: int, primary_id_pos: str = "B") -> str:
    """Detect company prefix by reading up to 10 active object IDs."""
    try:
        ids = await get_all_ids(service, spreadsheet_id, sheet_name, data_start_row, primary_id_pos)
        ids = ids[:10]
        for rid in ids:
            parts = rid.split("-")
            if len(parts) >= 3 and parts[0] and not parts[0][0].isdigit():
                return parts[0]
        return ""
    except Exception:
        return ""


async def next_ricefw_id(
    service: Any, 
    spreadsheet_id: str, 
    sheet_name: str, 
    module: str, 
    prefix: Optional[str], 
    data_start_row: int, 
    primary_id_pos: str = "B"
) -> str:
    """Generate the next sequentially incremented RICEFW ID for a module."""
    actual_prefix = prefix if prefix is not None else await detect_prefix(service, spreadsheet_id, sheet_name, data_start_row, primary_id_pos)
    all_ids = await get_all_ids(service, spreadsheet_id, sheet_name, data_start_row, primary_id_pos)
    
    nums = []
    module_upper = module.strip().upper()
    prefix_upper = actual_prefix.strip().upper() if actual_prefix else ""

    for rid in all_ids:
        parts = [p.strip() for p in rid.split("-") if p.strip()]
        if len(parts) >= 3:
            curr_prefix = parts[0].upper()
            curr_module = parts[1].upper()
            curr_num_str = parts[-1]
            if (not prefix_upper or curr_prefix == prefix_upper) and curr_module == module_upper and curr_num_str.isdigit():
                nums.append(int(curr_num_str))
        elif len(parts) == 2:
            curr_module = parts[0].upper()
            curr_num_str = parts[1]
            if curr_module == module_upper and curr_num_str.isdigit():
                nums.append(int(curr_num_str))

    next_num = (max(nums) + 1) if nums else 1
    if prefix_upper:
        return f"{prefix_upper}-{module_upper}-{next_num:03d}"
    else:
        return f"{module_upper}-{next_num:03d}"


async def switch_module(
    spreadsheet_id: str, 
    tab_name: str, 
    db: AsyncSession, 
    user_email: str, 
    session_id: Any,
    service: Any = None
) -> dict:
    """
    Validates tab existence if the service is supplied, then updates the
    active workspace session active_tab column.
    """
    tab_name_clean = tab_name.strip()
    
    # Optional tab existence check
    if service:
        try:
            meta = await _with_retry(lambda: service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute())
            tabs = [sheet["properties"]["title"] for sheet in meta.get("sheets", [])]
            if tab_name_clean not in tabs:
                return {"ok": False, "error": f"Tab '{tab_name_clean}' does not exist in spreadsheet."}
        except Exception as e:
            logger.warning(f"Could not verify tab existence on switch: {e}")

    # Update session in DB
    result = await db.execute(select(UserSession).where(UserSession.id == session_id))
    user_sess = result.scalar()
    if not user_sess:
        return {"ok": False, "error": "Active session not found."}

    user_sess.active_tab = tab_name_clean
    await db.commit()

    return {
        "ok": True,
        "active_tab": tab_name_clean,
        "message": f"Successfully switched to tab: {tab_name_clean}"
    }
