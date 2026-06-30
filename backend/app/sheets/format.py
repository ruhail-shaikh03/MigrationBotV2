import logging
from typing import Any
from app.sheets.retry import _with_retry
from app.sheets.meta import get_sheet_id, get_header_row
from app.sheets.read import find_row_num
from app.core.column_mapper import resolve_column

logger = logging.getLogger("sheets_format")

COLOR_MAP = {
    "red":   {"red": 0.96, "green": 0.80, "blue": 0.80},
    "green": {"red": 0.78, "green": 0.93, "blue": 0.78},
    "amber": {"red": 1.0,  "green": 0.90, "blue": 0.60},
    "blue":  {"red": 0.78, "green": 0.87, "blue": 0.96},
    "white": {"red": 1.0,  "green": 1.0,  "blue": 1.0 },
}

async def format_row(
    service: Any,
    spreadsheet_id: str,
    sheet_tab: str,
    ricefw_id: str,
    color: str,
    scope: str,
    schema_config: dict
) -> dict:
    """
    Applies background color highlight to a spreadsheet row or color cell.
    Uses batchUpdate to execute coloring on Google Sheets cells.
    """
    # Resolve tab-specific schema if using the new multi-tab format
    tab_schema = schema_config.get("tabs", {}).get(sheet_tab, {}) if "tabs" in schema_config else schema_config
    data_start_row = tab_schema.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = tab_schema.get("primary_id_position", "B")
    column_map = tab_schema.get("column_map") or schema_config.get("column_map")

    row_num = await find_row_num(service, spreadsheet_id, sheet_tab, ricefw_id, data_start_row, primary_id_pos)
    if row_num is None:
        return {"ok": False, "error": f"RICEFW ID '{ricefw_id}' not found."}

    rgb = COLOR_MAP.get(color.lower().strip(), COLOR_MAP["white"])
    sheet_id = await get_sheet_id(service, spreadsheet_id, sheet_tab)

    headers = await get_header_row(service, spreadsheet_id, sheet_tab, header_row_num)
    col_idx = {h.lower().strip(): i for i, h in enumerate(headers)}

    if scope == "entire_row":
        start_col = 0
        end_col = len(headers) if headers else 50
    else: # color_column_only
        # Resolve Color column location
        color_col = resolve_column("Color", column_map) or "Color "
        c_idx = col_idx.get(color_col.lower().strip())
        if c_idx is None:
            # Try plain "color"
            c_idx = col_idx.get("color")
        if c_idx is None:
            return {"ok": False, "error": "Color column could not be identified in sheet headers."}
        start_col = c_idx
        end_col = c_idx + 1

    body = {
        "requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": start_col,
                    "endColumnIndex": end_col
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": rgb
                    }
                },
                "fields": "userEnteredFormat.backgroundColor"
            }
        }]
    }

    await _with_retry(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=body
    ).execute())

    return {
        "ok": True, 
        "formatted": ricefw_id, 
        "color": color, 
        "scope": scope
    }
