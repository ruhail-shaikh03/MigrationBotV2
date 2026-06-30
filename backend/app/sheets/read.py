import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.sheets.retry import _with_retry
from app.sheets.meta import _detect_header_row, get_header_row
from app.core.column_mapper import resolve_column
from app.core.data_quality import DataQualityChecker
from app.models.audit_log import AuditLog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sheets_read")

def _get_tab_schema(schema_config: dict, active_tab: str) -> dict:
    if "tabs" in schema_config:
        return schema_config.get("tabs", {}).get(active_tab, {})
    return schema_config

def idx_to_col_letter(idx: int) -> str:
    """Helper to convert a 0-based column index to an A-Z sheet column letter."""
    result = ""
    while idx >= 0:
        result = chr(idx % 26 + ord("A")) + result
        idx = idx // 26 - 1
    return result


async def find_row_num(
    service: Any, 
    spreadsheet_id: str, 
    sheet_name: str, 
    ricefw_id: str, 
    data_start_row: int,
    primary_id_pos: str = "B"
) -> Optional[int]:
    """Scans the ID column to map a RICEFW ID string to its spreadsheet row index."""
    key = ricefw_id.strip().upper()
    col = primary_id_pos or "B"
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{col}{data_start_row}:{col}"
    ).execute())
    
    for i, row in enumerate(result.get("values", [])):
        if row and str(row[0]).strip().upper() == key:
            return data_start_row + i
    return None


async def get_row_raw(
    spreadsheet_id: str,
    active_tab: str,
    ricefw_id: str,
    fields: List[str],
    schema_config: dict,
    service: Any
) -> Dict[str, str]:
    """Helper to fetch a single row's current values for auditing."""
    schema_config = _get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = schema_config.get("primary_id_position", "B")

    row_num = await find_row_num(service, spreadsheet_id, active_tab, ricefw_id, data_start_row, primary_id_pos)
    if row_num is None:
        return {}

    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{active_tab}!{row_num}:{row_num}"
    ).execute())
    
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    values = result.get("values", [[]])[0]
    
    row_data = {h: (values[i] if i < len(values) else "") for i, h in enumerate(headers)}
    return {f: str(row_data.get(f, "")) for f in fields}


async def get_bulk_rows_raw(
    spreadsheet_id: str,
    active_tab: str,
    args: dict,
    schema_config: dict,
    service: Any
) -> Dict[str, Dict[str, str]]:
    """Helper to fetch multiple rows' current values for auditing in bulk updates."""
    schema_config = _get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = schema_config.get("primary_id_position", "B")
    primary_id_col = schema_config.get("primary_id_column", "RICEFW ID")

    target_ids = args.get("ricefw_ids") or []
    set_field = args.get("set_field", "")

    if not target_ids and args.get("filter_by"):
        # We need to scan the sheets to find matching target ids
        search_res = await search_rows(
            spreadsheet_id=spreadsheet_id,
            active_tab=active_tab,
            filters=[args["filter_by"]],
            return_fields=[primary_id_col, set_field],
            limit=500,
            schema_config=schema_config,
            column_map={},
            service=service
        )
        target_ids = [str(r.get(primary_id_col)) for r in search_res.get("rows", [])]

    # Pre-read the current cell values
    results = {}
    for rid in target_ids:
        val = await get_row_raw(spreadsheet_id, active_tab, rid, [set_field], schema_config, service)
        if val:
            results[rid] = val
    return results


async def get_row(
    spreadsheet_id: str,
    active_tab: str,
    ricefw_id: str,
    schema_config: dict,
    column_map: dict,
    service: Any
) -> dict:
    """Fetch all values mapped to headers for a single RICEFW object ID."""
    schema_config = _get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = schema_config.get("primary_id_position", "B")

    row_num = await find_row_num(service, spreadsheet_id, active_tab, ricefw_id, data_start_row, primary_id_pos)
    if row_num is None:
        return {"ok": False, "error": f"RICEFW ID '{ricefw_id}' not found in active tab."}

    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{active_tab}!{row_num}:{row_num}"
    ).execute())
    
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    values = result.get("values", [[]])[0]
    
    row_data = {h: (values[i] if i < len(values) else "") for i, h in enumerate(headers)}
    return {"ok": True, "ricefw_id": ricefw_id, "data": row_data}


async def search_rows(
    spreadsheet_id: str,
    active_tab: str,
    filters: List[dict],
    return_fields: Optional[List[str]],
    limit: int,
    schema_config: dict,
    column_map: dict,
    service: Any
) -> dict:
    """Scans the spreadsheet and filters rows based on a set of criteria (AND matching)."""
    schema_config = _get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)

    # Defaults fields to return if none specified
    critical_fields = schema_config.get("critical_fields", [])
    if not critical_fields:
        critical_fields = ["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Technical Resource "]
    
    return_fields = return_fields or [f for f in critical_fields if f in headers]

    col_idx = {h: i for i, h in enumerate(headers)}
    resolved_filters = []
    
    for f in filters:
        term = f.get("field", "")
        # Resolve user natural term to actual header name
        canonical = resolve_column(term, column_map) or term
        if canonical not in col_idx:
            return {"ok": False, "error": f"Column '{term}' could not be mapped to sheet headers."}
        resolved_filters.append({
            "idx": col_idx[canonical],
            "value": str(f.get("value", "")).strip(),
            "match_type": f.get("match_type", "exact")
        })

    # Read up to 2000 rows in one bulk get
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{active_tab}!{data_start_row}:{data_start_row + 2000}"
    ).execute())
    
    all_rows = result.get("values", [])
    matches = []

    for row in all_rows:
        padded = row + [""] * (len(headers) - len(row))
        
        # Verify if padded matches resolved filters
        match_ok = True
        for rf in resolved_filters:
            cell_val = str(padded[rf["idx"]]).strip()
            val_to_match = rf["value"]
            mtype = rf["match_type"]

            if mtype == "blank":
                if cell_val != "":
                    match_ok = False
                    break
            elif mtype == "contains":
                if val_to_match.lower() not in cell_val.lower():
                    match_ok = False
                    break
            else: # exact
                if cell_val.lower() != val_to_match.lower():
                    match_ok = False
                    break
        
        if not match_ok:
            continue

        # Extract only requested return fields
        row_dict = {f: padded[col_idx[f]] for f in return_fields if f in col_idx}
        matches.append(row_dict)
        
        if len(matches) >= limit:
            break

    return {
        "ok": True,
        "count": len(matches),
        "rows": matches,
        "capped": len(matches) == limit
    }


async def summarize(
    spreadsheet_id: str,
    active_tab: str,
    args: dict,
    schema_config: dict,
    column_map: dict,
    service: Any
) -> dict:
    """Calculates report figures, counts, and completion metrics across a sheet."""
    schema_config = _get_tab_schema(schema_config, active_tab)
    report_type = args.get("report_type")
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    col_idx = {h: i for i, h in enumerate(headers)}

    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{active_tab}!{data_start_row}:{data_start_row + 2000}"
    ).execute())
    all_rows = result.get("values", [])

    # Filter by module if scope_module is supplied
    scope_module = args.get("scope_module")
    module_col = schema_config.get("module_column", "Module")
    mod_idx = col_idx.get(module_col)

    rows = []
    for r in all_rows:
        padded = r + [""] * (len(headers) - len(r))
        if scope_module and mod_idx is not None:
            if str(padded[mod_idx]).strip().upper() != scope_module.strip().upper():
                continue
        rows.append(padded)

    total = len(rows)

    if report_type == "count_by_field":
        group_by = args.get("group_by_field", "")
        canonical = resolve_column(group_by, column_map) or group_by
        if canonical not in col_idx:
            return {"ok": False, "error": f"Grouping column '{group_by}' not found."}
            
        idx = col_idx[canonical]
        counts = {}
        for r in rows:
            val = str(r[idx]).strip() or "(blank)"
            counts[val] = counts.get(val, 0) + 1
            
        sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
        return {
            "ok": True,
            "report": "count_by_field",
            "field": canonical,
            "scope": scope_module or "all modules",
            "total_rows": total,
            "breakdown": [{"value": v, "count": c} for v, c in sorted_counts]
        }

    elif report_type == "completion_rate":
        comp_field = args.get("completion_field", "")
        comp_val = args.get("completion_value", "Completed")
        
        canonical = resolve_column(comp_field, column_map) or comp_field
        if canonical not in col_idx:
            return {"ok": False, "error": f"Completion status column '{comp_field}' not found."}

        idx = col_idx[canonical]
        done = sum(1 for r in rows if str(r[idx]).strip().lower() == comp_val.strip().lower())
        blank = sum(1 for r in rows if not str(r[idx]).strip())
        pct = round((done / total * 100), 1) if total else 0

        return {
            "ok": True,
            "report": "completion_rate",
            "field": canonical,
            "target_value": comp_val,
            "scope": scope_module or "all modules",
            "total_rows": total,
            "completed": done,
            "not_completed": total - done,
            "blank": blank,
            "completion_pct": pct
        }

    elif report_type == "blank_fields":
        blank_field = args.get("blank_field", "")
        canonical = resolve_column(blank_field, column_map) or blank_field
        if canonical not in col_idx:
            return {"ok": False, "error": f"Column '{blank_field}' not found."}

        idx = col_idx[canonical]
        id_col = schema_config.get("primary_id_column", "RICEFW ID")
        id_idx = col_idx.get(id_col, 0)
        
        blanks = [str(r[id_idx]).strip() for r in rows if not str(r[idx]).strip()]
        return {
            "ok": True,
            "report": "blank_fields",
            "field": canonical,
            "scope": scope_module or "all modules",
            "total_rows": total,
            "blank_count": len(blanks),
            "blank_pct": round(len(blanks) / total * 100, 1) if total else 0,
            "ids": blanks[:50]
        }

    elif report_type == "overdue":
        date_cols = schema_config.get("date_columns", {})
        go_live_col = date_cols.get("go_live", "Go-Live Date")
        status_col = schema_config.get("status_column", "Dev Status")
        id_col = schema_config.get("primary_id_column", "RICEFW ID")
        
        date_idx = col_idx.get(go_live_col)
        status_idx = col_idx.get(status_col)
        id_idx = col_idx.get(id_col, 0)

        if date_idx is None:
            return {"ok": False, "error": f"Date column '{go_live_col}' not found."}

        today = datetime.today().date()
        overdue = []

        done_statuses = {"complete", "completed", "done", "closed", "retired"}

        for r in rows:
            status = str(r[status_idx]).strip().lower() if status_idx is not None else ""
            if status in done_statuses:
                continue
            raw_date = str(r[date_idx]).strip()
            if not raw_date:
                continue
                
            for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                try:
                    go_live = datetime.strptime(raw_date, fmt).date()
                    if go_live < today:
                        overdue.append({
                            "id": str(r[id_idx]).strip(),
                            "go_live_date": raw_date,
                            "dev_status": str(r[status_idx]).strip() if status_idx is not None else "",
                            "days_overdue": (today - go_live).days
                        })
                    break
                except ValueError:
                    continue

        overdue.sort(key=lambda x: -x["days_overdue"])
        return {
            "ok": True,
            "report": "overdue",
            "scope": scope_module or "all modules",
            "total_rows": total,
            "overdue_count": len(overdue),
            "items": overdue[:30]
        }

    return {"ok": False, "error": f"Unknown report_type: {report_type}"}


async def run_data_quality_check(
    spreadsheet_id: str,
    active_tab: str,
    args: dict,
    schema_config: dict,
    db_session: AsyncSession,
    service: Any
) -> dict:
    """Executes the DataQualityChecker rules engine against spreadsheet data."""
    schema_config = _get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{active_tab}!{data_start_row}:{data_start_row + 2000}"
    ).execute())
    
    rows = result.get("values", [])
    checker = DataQualityChecker(headers, rows, schema_config)

    # Gather registered emails for RBAC mismatch checks
    # Select from users
    users_res = await db_session.execute(select(AuditLog.user_email).distinct())
    registered_emails = [r[0] for r in users_res.all() if r[0]]

    alerts = checker.consistency_checks(registered_emails)
    comp_score = checker.completeness_score()
    
    # Staleness evaluation
    stale_threshold = args.get("stale_threshold_days", 30)
    # Fetch recent audit entries for the current spreadsheet to calculate stales
    audit_res = await db_session.execute(
        select(AuditLog.ricefw_id, AuditLog.timestamp)
        .where(AuditLog.spreadsheet_id == spreadsheet_id, AuditLog.sheet_tab == active_tab)
    )
    audit_entries = [{"ricefw_id": r[0], "timestamp": r[1]} for r in audit_res.all()]
    
    stale_objs = checker.stale_items(audit_entries, threshold_days=stale_threshold)

    return {
        "ok": True,
        "completeness_score": comp_score,
        "alerts": alerts,
        "stale_items": stale_objs
    }
