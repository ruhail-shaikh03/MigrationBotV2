import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from app.sheets.retry import _with_retry
from app.sheets.meta import get_header_row, get_id_row_map
from app.core.column_mapper import resolve_column
from app.core.data_quality import DataQualityChecker
from app.core.overdue import is_finished_status
from app.core.people import parse_date
from app.core.schema import get_due_column, get_people_columns, get_tab_schema
from app.models.audit_log import AuditLog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sheets_read")

def idx_to_col_letter(idx: int) -> str:
    """Helper to convert a 0-based column index to an A-Z sheet column letter."""
    result = ""
    while idx >= 0:
        result = chr(idx % 26 + ord("A")) + result
        idx = idx // 26 - 1
    return result

# Sanity ceiling well above any real WRICEF tracker — guards _fetch_all_rows against
# runaway pagination on a misconfigured schema (e.g. a data_start_row that lands on a
# row that never actually goes blank). No real deployment should ever hit this.
_MAX_SCAN_ROWS = 20000


async def _fetch_all_rows(
    service: Any, spreadsheet_id: str, active_tab: str, data_start_row: int, page_size: int = 2000
) -> Tuple[List[List[str]], bool]:
    """Reads every data row from data_start_row onward, paging in page_size-row
    chunks instead of one fixed-size window. Previously search_rows/summarize/
    run_data_quality_check each did a single `{start}:{start+2000}` fetch, so a
    tracker larger than that was silently truncated (§16 history) — search_rows
    reported fewer matches, summarize computed percentages over a partial
    denominator, and data_quality scored a subset, all with no indication to the
    caller. Returns (rows, truncated); truncated is only True if _MAX_SCAN_ROWS is
    hit, which is a safety valve, not the normal path."""
    all_rows: List[List[str]] = []
    start = data_start_row
    while True:
        result = await _with_retry(lambda s=start: service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{active_tab}!{s}:{s + page_size - 1}"
        ).execute())
        page = result.get("values", [])
        all_rows.extend(page)

        if len(all_rows) >= _MAX_SCAN_ROWS:
            return all_rows[:_MAX_SCAN_ROWS], True
        if len(page) < page_size:
            return all_rows, False
        start += page_size


def _column_letter_to_index(letter: str) -> int:
    """"B" -> 1, "AA" -> 26. The same addressing `find_row_num` uses.

    Deliberately keyed off `primary_id_position` (a column letter) rather than
    `primary_id_column` (a header name), so the cached lookup resolves to exactly the
    cell the uncached one would. The two can disagree on a mis-detected schema, and a
    cache that quietly answers with a *different* row than the live path is a far worse
    failure than a slow one.
    """
    index = 0
    for char in str(letter or "B").strip().upper():
        if char.isalpha():
            index = index * 26 + (ord(char) - 64)
    return max(index - 1, 0)


async def _cached_matrix(
    service: Any, spreadsheet_id: str, active_tab: str, data_start_row: int
) -> Tuple[List[str], List[List[str]], bool]:
    """The tab's headers and data rows, from the 60-second Redis cache when warm.

    Local import: `rows_cache` imports `_fetch_all_rows` from this module, so importing it
    at module scope would be a cycle. Same pattern `core/people.py` uses for its
    `PersonResolver` import.

    Why the agent reads go through the dashboard's cache at all: every tool call used to
    re-scan the whole tab. A single agentic turn that runs summarize, then get_row, then
    search_rows scanned the same 412 rows three times over, each scan paging up to
    `_MAX_SCAN_ROWS`. The cache already invalidates on write (`queue/worker.py` calls
    `invalidate_tab` after every applied job), so this does not make the agent any staler
    than the grid a user is looking at while they ask.
    """
    from app.sheets.rows_cache import get_tab_matrix

    return await get_tab_matrix(service, spreadsheet_id, active_tab, data_start_row)


async def find_all_row_nums(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    ricefw_id: str,
    data_start_row: int,
    primary_id_pos: str = "B"
) -> List[int]:
    """Every spreadsheet row whose ID column holds this ID, in sheet order.

    Returns a list rather than the first hit because a tracker's ID column is not
    guaranteed unique — 27 of 412 rows on the reference sheet share an ID (§16.7) —
    and a caller that cannot see the duplication cannot refuse to act on it.
    """
    key = ricefw_id.strip().upper()
    col = primary_id_pos or "B"
    result = await _with_retry(lambda: service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!{col}{data_start_row}:{col}"
    ).execute())

    return [
        data_start_row + i
        for i, row in enumerate(result.get("values", []))
        if row and str(row[0]).strip().upper() == key
    ]


async def find_row_num(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    ricefw_id: str,
    data_start_row: int,
    primary_id_pos: str = "B"
) -> Optional[int]:
    """The first row matching this ID, or None.

    Kept for read-side callers that only need *a* row. Write paths must use
    `find_all_row_nums` and refuse on more than one match — silently taking the first
    is precisely the bug §16.7 records.
    """
    matches = await find_all_row_nums(
        service, spreadsheet_id, sheet_name, ricefw_id, data_start_row, primary_id_pos
    )
    return matches[0] if matches else None


async def cached_id_row_nums(
    spreadsheet_id: str,
    active_tab: str,
    ricefw_id: str,
    schema_config: dict,
    service: Any
) -> List[int]:
    """Rows matching this ID, answered from the cache tiers rather than a fresh scan.

    Used only to refuse an ambiguous write early, so the user hears about it at dispatch
    instead of as a queue failure seconds later. Advisory by construction: the cache can
    be up to 60s stale, and the authoritative check is the live scan the worker already
    performs inside `write.update_cell` (§16.7). Costs zero Sheets calls whenever the tab
    is already cached, which is why the guard can be unconditional.
    """
    tab_schema = get_tab_schema(schema_config, active_tab)
    data_start_row = tab_schema.get("data_start_row", 3)
    id_idx = _column_letter_to_index(tab_schema.get("primary_id_position", "B"))

    _, rows, _ = await _cached_matrix(service, spreadsheet_id, active_tab, data_start_row)

    key = ricefw_id.strip().upper()
    return [
        data_start_row + i
        for i, row in enumerate(rows)
        if id_idx < len(row) and str(row[id_idx]).strip().upper() == key
    ]


async def get_row_raw(
    spreadsheet_id: str,
    active_tab: str,
    ricefw_id: str,
    fields: List[str],
    schema_config: dict,
    service: Any,
    row_number: Optional[int] = None
) -> Dict[str, str]:
    """Helper to fetch a single row's current values for auditing.

    `row_number` reads that physical row instead of resolving the ID. The dashboard passes
    it so the audit "before" value describes the row the write will actually land on — on
    a duplicated ID the two are not the same row, and an audit entry describing the wrong
    one is what undo would later try to restore (§10.5, §16.7).
    """
    schema_config = get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    header_row_num = data_start_row - 1
    primary_id_pos = schema_config.get("primary_id_position", "B")

    row_num = row_number
    if row_num is None:
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
    service: Any,
    column_map: Optional[dict] = None
) -> Dict[str, Dict[str, str]]:
    """Helper to fetch multiple rows' current values for auditing in bulk updates.
    Previously this cost ~3 Sheets calls per target ID (find_row_num's own full
    ID-column scan, the row fetch, and a header re-fetch, all inside get_row_raw) —
    a 50-row bulk update's audit pre-read alone was ~150 calls. Now: one ID-column
    scan, one header fetch, and one batchGet covering every resolved row, regardless
    of how many targets there are."""
    tab_schema = get_tab_schema(schema_config, active_tab)
    # Resolve the same column map the write path uses (see write.py:bulk_update), so a
    # filter_by targeting a natural-language field name resolves to the identical rows.
    column_map = column_map or tab_schema.get("column_map") or schema_config.get("column_map") or {}

    schema_config = tab_schema
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
            column_map=column_map,
            service=service
        )
        target_ids = [str(r.get(primary_id_col)) for r in search_res.get("rows", [])]

    if not target_ids:
        return {}

    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    id_row_map = await get_id_row_map(service, spreadsheet_id, active_tab, data_start_row, primary_id_pos)

    # Only unambiguous IDs get an audit "before" value. An ID matching several rows is
    # one bulk_update will refuse outright, and reading one of its rows here would put a
    # "before" value in the audit trail for a row that was never written — which is the
    # value undo would later try to restore (§10.5).
    resolved: Dict[str, int] = {}
    for rid in target_ids:
        row_nums = id_row_map.get(rid.strip().upper()) or []
        if len(row_nums) == 1:
            resolved[rid] = row_nums[0]

    if not resolved:
        return {}

    ranges = [f"{active_tab}!{row_num}:{row_num}" for row_num in resolved.values()]
    batch_result = await _with_retry(lambda: service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id,
        ranges=ranges
    ).execute())
    value_ranges = batch_result.get("valueRanges", [])

    results: Dict[str, Dict[str, str]] = {}
    for rid, value_range in zip(resolved.keys(), value_ranges):
        values = value_range.get("values", [[]])
        row_values = values[0] if values else []
        row_data = {h: (row_values[i] if i < len(row_values) else "") for i, h in enumerate(headers)}
        results[rid] = {set_field: str(row_data.get(set_field, ""))}

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
    schema_config = get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    primary_id_pos = schema_config.get("primary_id_position", "B")

    # Three Sheets calls became zero on a warm cache. The uncached path scanned the whole
    # ID column to find the row, fetched that row, then fetched the headers — and the
    # model's most common mistake is calling this tool in a loop, which is exactly the
    # shape that used to blow the quota.
    headers, all_rows, _ = await _cached_matrix(service, spreadsheet_id, active_tab, data_start_row)

    id_index = _column_letter_to_index(primary_id_pos)
    key = ricefw_id.strip().upper()
    values = None
    for row in all_rows:
        if id_index < len(row) and str(row[id_index]).strip().upper() == key:
            values = row
            break

    if values is None:
        return {"ok": False, "error": f"RICEFW ID '{ricefw_id}' not found in active tab."}

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
    schema_config = get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    headers, all_rows, truncated = await _cached_matrix(
        service, spreadsheet_id, active_tab, data_start_row
    )

    # get_header_row strips each header, but resolve_column's canonical names are copied
    # verbatim from COLUMN_ALIASES / the LLM-built column_map and deliberately keep the
    # sheet's real trailing spaces (e.g. "Technical Resource "). Compare case/whitespace-
    # normalized on both sides — same pattern write.py already uses.
    col_idx = {h.lower().strip(): i for i, h in enumerate(headers)}

    # Defaults fields to return if none specified
    critical_fields = schema_config.get("critical_fields", [])
    if not critical_fields:
        critical_fields = ["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Technical Resource "]

    return_fields = return_fields or [f for f in critical_fields if f.lower().strip() in col_idx]

    resolved_filters = []

    for f in filters:
        term = f.get("field", "")
        # Resolve user natural term to actual header name
        canonical = resolve_column(term, column_map) or term
        norm_canonical = canonical.lower().strip()
        if norm_canonical not in col_idx:
            return {"ok": False, "error": f"Column '{term}' could not be mapped to sheet headers."}
        resolved_filters.append({
            "idx": col_idx[norm_canonical],
            "value": str(f.get("value", "")).strip(),
            "match_type": f.get("match_type", "exact")
        })

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
        row_dict = {f: padded[col_idx[f.lower().strip()]] for f in return_fields if f.lower().strip() in col_idx}
        matches.append(row_dict)
        
        if len(matches) >= limit:
            break

    return {
        "ok": True,
        "count": len(matches),
        "rows": matches,
        "capped": len(matches) == limit,
        "truncated": truncated
    }


async def summarize(
    spreadsheet_id: str,
    active_tab: str,
    args: dict,
    schema_config: dict,
    column_map: dict,
    service: Any,
    resolver: Any = None,
) -> dict:
    """Calculates report figures, counts, and completion metrics across a sheet.

    `resolver` is an optional `PersonResolver`. It only affects count_by_field on a
    people column, where it splits shared cells and applies the project's alias map so
    this agrees with the dashboard's workload panel.
    """
    schema_config = get_tab_schema(schema_config, active_tab)
    report_type = args.get("report_type")
    data_start_row = schema_config.get("data_start_row", 3)

    headers, all_rows, truncated = await _cached_matrix(
        service, spreadsheet_id, active_tab, data_start_row
    )
    # Same header-normalization as search_rows: headers are stripped, canonical names
    # from resolve_column/schema_config may not be.
    col_idx = {h.lower().strip(): i for i, h in enumerate(headers)}

    def _col(name: str):
        return col_idx.get(name.lower().strip()) if name else None

    # Filter by module if scope_module is supplied
    scope_module = args.get("scope_module")
    module_col = schema_config.get("module_column", "Module")
    mod_idx = _col(module_col)

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
        idx = _col(canonical)
        if idx is None:
            return {"ok": False, "error": f"Grouping column '{group_by}' not found."}

        # When the grouping column is one of this tab's people columns, count resolved
        # people rather than raw cells — otherwise chat reports "Minhaj Alam & Dawood" as
        # a person while the dashboard beside it reports two, from the same sheet.
        people_headers = {p["header"].lower().strip() for p in get_people_columns(schema_config)}
        is_people_column = canonical.lower().strip() in people_headers

        counts = {}
        for r in rows:
            raw = str(r[idx]).strip()
            if is_people_column and resolver is not None:
                for name in (resolver.resolve_cell(raw) or ["(blank)"]):
                    counts[name] = counts.get(name, 0) + 1
            else:
                val = raw or "(blank)"
                counts[val] = counts.get(val, 0) + 1

        sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
        return {
            "ok": True,
            "report": "count_by_field",
            "field": canonical,
            "scope": scope_module or "all modules",
            "total_rows": total,
            "truncated": truncated,
            "breakdown": [{"value": v, "count": c} for v, c in sorted_counts]
        }

    elif report_type == "completion_rate":
        comp_field = args.get("completion_field", "")
        comp_val = args.get("completion_value", "Completed")
        
        canonical = resolve_column(comp_field, column_map) or comp_field
        idx = _col(canonical)
        if idx is None:
            return {"ok": False, "error": f"Completion status column '{comp_field}' not found."}

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
            "truncated": truncated,
            "completed": done,
            "not_completed": total - done,
            "blank": blank,
            "completion_pct": pct
        }

    elif report_type == "blank_fields":
        blank_field = args.get("blank_field", "")
        canonical = resolve_column(blank_field, column_map) or blank_field
        idx = _col(canonical)
        if idx is None:
            return {"ok": False, "error": f"Column '{blank_field}' not found."}

        id_col = schema_config.get("primary_id_column", "RICEFW ID")
        id_idx = _col(id_col)
        if id_idx is None:
            return {"ok": False, "error": f"Primary ID column '{id_col}' not found; cannot report row IDs."}

        blanks = [str(r[id_idx]).strip() for r in rows if not str(r[idx]).strip()]
        return {
            "ok": True,
            "report": "blank_fields",
            "field": canonical,
            "scope": scope_module or "all modules",
            "total_rows": total,
            "truncated": truncated,
            "blank_count": len(blanks),
            "blank_pct": round(len(blanks) / total * 100, 1) if total else 0,
            "ids": blanks[:50]
        }

    elif report_type == "overdue":
        # Resolved centrally so this and the dashboard agree on what a deadline is.
        # Note the schema is consulted with the *real* header list: on projects whose
        # detection predates the "due" slot, the deadline column is recovered by name
        # rather than reported as missing (core/schema.py:get_due_column).
        due_col = get_due_column(schema_config, headers)
        # `or` rather than a .get default: detection writes these keys with a literal
        # null when it finds nothing, and a null value ignores the default — the exact
        # trap that broke the date lookup here.
        status_col = schema_config.get("status_column") or "Dev Status"
        id_col = schema_config.get("primary_id_column") or "RICEFW ID"

        date_idx = _col(due_col)
        status_idx = _col(status_col)
        id_idx = _col(id_col)

        if date_idx is None:
            # Name the columns that do exist. A bare "not found" gave the model nothing
            # to correct against, so it fell back to guessing date substrings through
            # search_rows until the loop hit its iteration cap.
            return {
                "ok": False,
                "error": (
                    "This sheet has no due-date column mapped, so overdue cannot be "
                    "computed. Do not try to work it out with search_rows — answer that "
                    "the sheet records no deadline, and suggest an admin map one in "
                    f"/admin/projects. Columns present: {', '.join(h for h in headers if h)}."
                ),
            }
        if id_idx is None:
            return {"ok": False, "error": f"Primary ID column '{id_col}' not found; cannot report row IDs."}

        # Enough context to answer "what's overdue?" from this one result. Returning bare
        # IDs forced a get_row per item to find out what each one *was* — precisely the
        # per-row loop the system prompt forbids, with no supported alternative.
        desc_col = schema_config.get("description_column")
        desc_idx = _col(desc_col)
        people = get_people_columns(schema_config)
        people_idx = [(p["label"], _col(p["header"])) for p in people]

        today = datetime.today().date()
        overdue = []
        unparsed_dates = 0

        # Shared with the dashboard so the two surfaces cannot give different answers
        # (core/overdue.py). This was an exact set-membership test against
        # ["Complete", "Done", ...]; the reference sheet's status is "Completed", which
        # matches none of them, so all 171 finished rows were reported overdue while the
        # dashboard — matching on substrings — correctly excluded them.
        exclusions = args.get("overdue_status_exclusions") or None

        for r in rows:
            status = str(r[status_idx]).strip() if status_idx is not None else ""
            if is_finished_status(status, exclusions):
                continue
            raw_date = str(r[date_idx]).strip()
            if not raw_date:
                continue

            # Shared parser: the local format tuple here omitted the dotted form
            # (10.05.2026) that the reference sheet actually uses, so every date silently
            # failed to parse and the report returned zero overdue items.
            due = parse_date(raw_date)
            if due is None:
                unparsed_dates += 1
                continue
            if due >= today:
                continue

            item = {
                "id": str(r[id_idx]).strip(),
                "due_date": raw_date,
                "status": status,
                "days_overdue": (today - due).days,
            }
            if desc_idx is not None:
                item["description"] = str(r[desc_idx]).strip()[:120]
            for label, idx in people_idx:
                if idx is not None and str(r[idx]).strip():
                    item[label] = str(r[idx]).strip()
            overdue.append(item)

        overdue.sort(key=lambda x: -x["days_overdue"])
        return {
            "ok": True,
            "report": "overdue",
            "date_column": due_col,
            "scope": scope_module or "all modules",
            "total_rows": total,
            "truncated": truncated,
            "overdue_count": len(overdue),
            # Surfaced rather than swallowed: a sheet full of dates this parser cannot
            # read would otherwise report zero overdue items, which reads as good news.
            "unparsed_dates": unparsed_dates,
            "items": overdue[:30],
            "items_shown": min(len(overdue), 30),
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
    schema_config = get_tab_schema(schema_config, active_tab)
    data_start_row = schema_config.get("data_start_row", 3)
    headers, rows, truncated = await _cached_matrix(
        service, spreadsheet_id, active_tab, data_start_row
    )

    # Optional module scoping, same normalized-lookup pattern as summarize()
    scope_module = args.get("scope_module")
    if scope_module:
        col_idx = {h.lower().strip(): i for i, h in enumerate(headers)}
        module_col = schema_config.get("module_column", "Module")
        mod_idx = col_idx.get(module_col.lower().strip())
        if mod_idx is not None:
            rows = [
                r for r in rows
                if mod_idx < len(r) and str(r[mod_idx]).strip().upper() == scope_module.strip().upper()
            ]

    checker = DataQualityChecker(headers, rows, schema_config)
    check_type = args.get("check_type", "all")
    valid_check_types = {"blank_fields", "consistency", "stale", "completeness_score", "all"}
    if check_type not in valid_check_types:
        return {"ok": False, "error": f"Unknown check_type: {check_type}"}

    report: Dict[str, Any] = {
        "ok": True,
        "check_type": check_type,
        "scope": scope_module or "all modules",
        "truncated": truncated
    }

    if check_type in ("blank_fields", "all"):
        fields = args.get("fields") or schema_config.get("critical_fields") or [
            "RICEFW ID", "Module", "Type", "Description", "Dev Status", "Technical Resource "
        ]
        report["blank_field_counts"] = checker.blank_field_counts(fields)

    if check_type in ("consistency", "all"):
        # Gather registered emails for RBAC mismatch checks
        users_res = await db_session.execute(select(AuditLog.user_email).distinct())
        registered_emails = [r[0] for r in users_res.all() if r[0]]
        report["alerts"] = checker.consistency_checks(registered_emails)

    if check_type in ("completeness_score", "all"):
        report["completeness_score"] = checker.completeness_score()

    if check_type in ("stale", "all"):
        # tool_schemas.py declares this arg as `threshold_days`, not `stale_threshold_days`
        stale_threshold = args.get("threshold_days", 30)
        audit_res = await db_session.execute(
            select(AuditLog.ricefw_id, AuditLog.timestamp)
            .where(AuditLog.spreadsheet_id == spreadsheet_id, AuditLog.sheet_tab == active_tab)
        )
        audit_entries = [{"ricefw_id": r[0], "timestamp": r[1]} for r in audit_res.all()]
        report["stale_items"] = checker.stale_items(audit_entries, threshold_days=stale_threshold)

    return report
