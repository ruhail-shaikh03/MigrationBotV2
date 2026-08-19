import logging
from typing import List, Any, Optional
from app.sheets.retry import _with_retry
from app.sheets.meta import get_header_row, get_id_row_map
from app.sheets.read import find_all_row_nums, idx_to_col_letter, search_rows
from app.core.column_mapper import resolve_column
from app.core.errors import ambiguous_id_result
from app.core.schema import get_tab_schema

logger = logging.getLogger("sheets_write")

async def update_cell(
    service: Any,
    spreadsheet_id: str,
    sheet_tab: str,
    ricefw_id: str,
    updates: List[dict],
    schema_config: dict,
    row_number: Optional[int] = None,
) -> dict:
    """
    Updates one or more fields for a specific RICEFW ID.
    Groups updates into a single batchUpdate operation to minimize API latency and quota consumption.

    `row_number` pins the target to one physical row. Only a caller that genuinely knows
    which row the user meant may pass it — the dashboard does, because the user clicked a
    rendered grid row (api/dashboard.py:patch_row). The agent never does: it has an ID and
    nothing else, so an ID matching several rows is refused rather than guessed at.
    """
    # Resolve tab-specific schema if using the new multi-tab format
    tab_schema = get_tab_schema(schema_config, sheet_tab)
    data_start_row = tab_schema.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = tab_schema.get("primary_id_position", "B")
    column_map = tab_schema.get("column_map") or schema_config.get("column_map") or {}

    row_nums = await find_all_row_nums(service, spreadsheet_id, sheet_tab, ricefw_id, data_start_row, primary_id_pos)
    if not row_nums:
        return {"ok": False, "error": f"RICEFW ID '{ricefw_id}' not found in active tab."}
    if row_number is not None:
        # The pin is verified against the live scan rather than trusted: a row that no
        # longer holds this ID means the sheet moved under the user between render and
        # write, and writing to it anyway would hit whatever now occupies that row.
        if row_number not in row_nums:
            return {
                "ok": False,
                "error": (
                    f"Row {row_number} no longer holds '{ricefw_id}' — the sheet changed. "
                    "Reload and try again."
                ),
            }
        row_num = row_number
    elif len(row_nums) > 1:
        return ambiguous_id_result(ricefw_id, row_nums, "update_cell")
    else:
        row_num = row_nums[0]

    headers = await get_header_row(service, spreadsheet_id, sheet_tab, header_row_num)
    col_idx = {h.lower().strip(): i for i, h in enumerate(headers)}

    data_items = []
    for item in updates:
        field = item.get("field", "")
        value = item.get("value", "")
        
        # Resolve column header
        canonical = resolve_column(field, column_map) or field
        c_idx = col_idx.get(canonical.lower().strip())
        if c_idx is None:
            return {"ok": False, "error": f"Column '{field}' not found in sheet headers."}
            
        col_letter = idx_to_col_letter(c_idx)
        data_items.append({
            "range": f"{sheet_tab}!{col_letter}{row_num}",
            "values": [[value]]
        })

    # Batch execute updates
    await _with_retry(lambda: service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": data_items
        }
    ).execute())

    return {
        "ok": True, 
        "ricefw_id": ricefw_id, 
        "updated": len(data_items), 
        "message": f"Successfully updated {len(data_items)} cells."
    }


async def bulk_update(
    service: Any,
    spreadsheet_id: str,
    sheet_tab: str,
    args: dict,
    schema_config: dict,
) -> dict:
    """
    Update a single field/value on multiple RICEFW items in a single API roundtrip.
    Targets items either by direct list of IDs or via filtering matching rows.
    """
    # Resolve tab-specific schema if using the new multi-tab format
    tab_schema = get_tab_schema(schema_config, sheet_tab)
    data_start_row = tab_schema.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = tab_schema.get("primary_id_position", "B")
    primary_id_col = tab_schema.get("primary_id_column", "RICEFW ID")
    column_map = tab_schema.get("column_map") or schema_config.get("column_map") or {}

    headers = await get_header_row(service, spreadsheet_id, sheet_tab, header_row_num)
    col_idx = {h.lower().strip(): i for i, h in enumerate(headers)}

    set_field = args.get("set_field", "")
    set_value = args.get("set_value", "")

    canonical_set_field = resolve_column(set_field, column_map) or set_field
    set_col_idx = col_idx.get(canonical_set_field.lower().strip())
    if set_col_idx is None:
        return {"ok": False, "error": f"Target column '{set_field}' not found."}

    target_ids = args.get("ricefw_ids") or []
    
    # Check if target rows should be resolved via filter_by conditions instead
    if not target_ids and args.get("filter_by"):
        filter_data = args["filter_by"]
        search_res = await search_rows(
            spreadsheet_id=spreadsheet_id,
            active_tab=sheet_tab,
            filters=[filter_data],
            return_fields=[primary_id_col],
            limit=500,
            schema_config=schema_config,
            column_map=column_map,
            service=service
        )
        target_ids = [str(r.get(primary_id_col)) for r in search_res.get("rows", [])]

    if not target_ids:
        return {"ok": True, "updated": 0, "message": "No matching target objects found for update."}

    # Resolve all row coordinates from one ID-column scan instead of one
    # find_row_num call (its own full-column scan) per target ID.
    id_row_map = await get_id_row_map(service, spreadsheet_id, sheet_tab, data_start_row, primary_id_pos)

    data_items = []
    not_found = []

    col_letter = idx_to_col_letter(set_col_idx)

    ambiguous = []

    for rid in target_ids:
        row_nums = id_row_map.get(rid.strip().upper()) or []
        if not row_nums:
            not_found.append({"id": rid, "error": "Not found in sheet"})
            continue
        if len(row_nums) > 1:
            # Skipped, not guessed. A bulk update is exactly where taking the first (or,
            # as this function used to, the last) match does the most damage: nobody is
            # reading 50 row numbers to notice one landed somewhere else.
            ambiguous.append({
                "id": rid,
                "error": ambiguous_id_result(rid, row_nums, "bulk_update")["error"],
            })
            continue
        data_items.append({
            "range": f"{sheet_tab}!{col_letter}{row_nums[0]}",
            "values": [[set_value]]
        })

    succeeded = []
    failed = list(not_found) + list(ambiguous)
    # Never written, so never "succeeded" — and never retried by the individual-write
    # fallback below, which would otherwise reintroduce the first-match guess.
    skipped = {f["id"] for f in failed}

    if data_items:
        try:
            # Batch execute updates in one roundtrip
            await _with_retry(lambda: service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": data_items
                }
            ).execute())
            
            succeeded = [rid for rid in target_ids if rid not in skipped]
        except Exception as e:
            # Fallback to individual writes if batchUpdate fails completely
            logger.warning(f"Batch update failed. Retrying cell writes individually: {e}")
            for rid in target_ids:
                if rid in skipped:
                    continue
                res = await update_cell(
                    service=service,
                    spreadsheet_id=spreadsheet_id,
                    sheet_tab=sheet_tab,
                    ricefw_id=rid,
                    updates=[{"field": canonical_set_field, "value": set_value}],
                    schema_config=schema_config
                )
                if res.get("ok"):
                    succeeded.append(rid)
                else:
                    failed.append({"id": rid, "error": res.get("error", "Write failed")})

    return {
        "ok": len(failed) == 0,
        "updated": len(succeeded),
        "succeeded": succeeded,
        "failed": failed
    }


async def add_row(
    service: Any,
    spreadsheet_id: str,
    sheet_tab: str,
    ricefw_id: str,
    module: str,
    type: str,
    description: str,
    assigned_to: str = "",
    fields: Optional[dict] = None,
    schema_config: dict = None,
) -> dict:
    """Appends a new WRICEF tracker object to the sheet under the correct columns."""
    raw_schema = schema_config or {}
    # Resolve tab-specific schema if using the new multi-tab format
    schema = get_tab_schema(raw_schema, sheet_tab)
    data_start_row = schema.get("data_start_row", 3)
    header_row_num = data_start_row - 1

    headers = await get_header_row(service, spreadsheet_id, sheet_tab, header_row_num)
    row_values = [""] * len(headers)

    primary_id_col = schema.get("primary_id_column", "RICEFW ID")
    module_col = schema.get("module_column", "Module")
    type_col = schema.get("type_column", "Type")
    description_col = schema.get("description_column", "Description")
    assignee_col = schema.get("assignee_column", "Technical Resource ")
    
    # Fallback to Assigned To if assignee_col isn't found
    if assignee_col not in headers and "Assigned To" in headers:
        assignee_col = "Assigned To"

    field_map = {
        primary_id_col: ricefw_id,
        module_col: module,
        type_col: type,
        description_col: description,
        assignee_col: assigned_to
    }
    if fields:
        field_map.update(fields)

    for col_name, val in field_map.items():
        if col_name in headers:
            row_values[headers.index(col_name)] = str(val)

    # Append call using insertDataOption=INSERT_ROWS
    await _with_retry(lambda: service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_tab}!A:A",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row_values]}
    ).execute())

    return {"ok": True, "added": ricefw_id}
