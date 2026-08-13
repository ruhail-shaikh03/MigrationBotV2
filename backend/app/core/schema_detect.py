import json
import logging
import re
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from app.core.column_mapper import build_column_map
from app.core.people import slugify_role

logger = logging.getLogger("schema_detect")

def parse_spreadsheet_url(url: str) -> str:
    """Extract spreadsheet_id from a Google Sheets URL or raw ID."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    cleaned = url.strip()
    if cleaned and "/" not in cleaned:
        return cleaned
    raise ValueError("Invalid Google Sheets URL or Spreadsheet ID")

async def detect_all_tabs(
    service: Any,
    spreadsheet_id: str,
    client: AsyncOpenAI
) -> Dict[str, Any]:
    """
    Fetch all tabs in the spreadsheet, analyze their raw first 10 rows using LLM reasoning,
    determine which tabs are trackers, locate their header rows, and build their schema_configs.
    """
    try:
        from app.sheets.retry import _with_retry
        meta = await _with_retry(lambda: service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute())
        tab_names = [sheet["properties"]["title"] for sheet in meta.get("sheets", [])]
    except Exception as e:
        logger.error(f"Failed to fetch spreadsheet metadata: {e}")
        raise ValueError(f"Could not access spreadsheet: {e}")

    consolidated_config = {}
    detected_tabs = []

    for tab in tab_names:
        try:
            # Range: read A1:Z10 (first 10 rows)
            result = await _with_retry(lambda: service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab}'!A1:Z10"
            ).execute())
            rows = result.get("values", [])
            if not rows:
                logger.info(f"Tab '{tab}' is empty. Skipping.")
                continue

            tab_schema = await detect_schema_config(
                tab_name=tab,
                raw_rows=rows,
                tab_names=tab_names,
                client=client
            )

            if not tab_schema.get("is_tracker_sheet", False):
                logger.info(f"LLM determined tab '{tab}' is not a tracker sheet. Skipping.")
                continue

            header_row_idx = tab_schema.get("header_row_index", 0)
            # data_start_row is 1-based index of row right after header row
            tab_schema["data_start_row"] = header_row_idx + 2

            # Generate this tab's own alias map from its real headers, rather than
            # leaving every deployment on the static COLUMN_ALIASES fallback (hardcoded
            # to one customer's sheet — TDD §7.3, and the root of the §16.2 whitespace
            # bug class). build_column_map falls back to COLUMN_ALIASES itself on any
            # LLM failure, so this can't make detection worse than before.
            if header_row_idx < len(rows):
                header_row = [str(c).strip() for c in rows[header_row_idx]]
                tab_schema["column_map"] = await build_column_map(header_row, client)

            consolidated_config[tab] = tab_schema
            if tab not in detected_tabs:
                detected_tabs.append(tab)
                
        except Exception as te:
            logger.warning(f"Error auto-detecting schema for tab '{tab}': {te}")
            continue
            
    return {
        "tabs": consolidated_config,
        "global": {
            # Tab names only. This used to be emitted as "valid_modules", which
            # schema.py then fed into the prompt as an allowlist of legal ID prefixes —
            # conflating "tabs you can switch to" with "identifiers that may exist".
            # Consumers read tab names from `tabs` directly (schema.py:get_available_tabs);
            # this stays purely informational.
            "detected_tabs": detected_tabs,
            "company_prefix": ""
        }
    }


SCHEMA_DETECTION_PROMPT = """
You are an expert data architect analyzing raw rows from a Google Sheet tab to determine if it is a project/migration tracking table and map its columns semantically.

Tab Name: {tab_name}
All Tab Names in Spreadsheet: {tab_names_json}

First 10 Raw Rows from this tab (each row is a list of cell values):
{raw_rows_json}

Your goals:
1. Determine if this tab is a data tracking sheet (contains a structured list of work items, tasks, RICEFW objects, deliverables, or requirements). If it is a summary cover page, dashboard, notes page, or empty sheet, set "is_tracker_sheet": false.
2. Identify the 0-based row index that contains the column headers (e.g., 0 if headers are in row 1, 1 if in row 2, etc.).
3. Semantically map the sheet's exact column header names to our system's roles based on MEANING (concept similarity), NOT literal word matching:
   - "primary_id_column": Header for unique item ID (e.g., RICEFW ID, Task #, Object Code, Req ID, Item ID, ID). MUST NOT BE NULL if is_tracker_sheet is true.
   - "primary_id_position": Column letter (e.g., "A", "B", "C") matching the 0-indexed position of primary_id_column in the header row.
   - "status_column": Header tracking progress/status (e.g., Dev Status, State, Progress, Stage, Status). Set to null if missing.
   - "module_column": Header for functional area/module/workstream (e.g., Module, Area, Functional Code, Track). Set to null if missing.
   - "people_columns": A LIST of EVERY column naming a person responsible for the item. A sheet
     routinely has more than one (e.g. a technical resource AND a functional resource, or a
     developer, a reviewer and a business owner) — return all of them, not just the first.
     Each entry is {{"label": "<the sheet's own wording, cleaned of stray whitespace>",
     "header": "<the header copied VERBATIM including any trailing space or typo>"}}.
     Order them as they appear in the sheet. Return [] if the sheet names no people.
     Do NOT invent a role the sheet does not have, and do NOT rename a role to a
     conventional term — if the header says "Functinal Resource", the label is
     "Functinal Resource", not "Functional Consultant".
   - "assignee_column": The header of the FIRST entry in people_columns, or null if there are
     none. Retained for backward compatibility with existing tools.
   - "description_column": Header for details/description (e.g., Description, Task Name, Summary, Details). Set to null if missing.
   - "type_column": Header for category/type (e.g., Type, RICEFW Type, Object Type, Category). Set to null if missing.
   - "effort_columns": A LIST of columns expressing level of effort / duration / estimated
     work (e.g. Est. Days, Effort, Man-Days, Duration, Story Points). Each entry is
     {{"label": "<the sheet's own wording>", "header": "<verbatim header>",
     "unit": "<days|hours|points, whichever the sheet means>"}}. Return [] if absent —
     many trackers record no effort at all, and [] is the correct answer, not a guess.
   - "date_columns": Object mapping "due", "go_live", "signoff", "start", "completion" to
     their respective headers, or null if missing. "due" is the date the row is *supposed*
     to be finished by — the deadline everything overdue is measured against — and is often
     worded "Expected Completion Date", "Target Date", "Planned Finish" or "ETA". Keep it
     distinct from "completion", which is the date work *actually* finished: a sheet
     frequently has both, and confusing them reports finished work as overdue. If only one
     such column exists, decide from its wording which it is; "expected"/"planned"/"target"
     means "due".

Every value below must be copied verbatim from THIS tab's header row, preserving exact
spelling, casing and any trailing spaces. Never substitute a conventional name for what the
sheet actually says. Do NOT emit lists of permitted identifiers, module codes or type codes:
the sheet's data defines its own vocabulary, and an invented allowlist is later enforced as a
constraint that rejects legitimate rows.

The structure below is an illustrative shape only — its values come from an unrelated sheet
and must not be copied:
{{
  "is_tracker_sheet": true,
  "header_row_index": 0,
  "primary_id_column": "Ticket Ref",
  "primary_id_position": "B",
  "status_column": "Current State",
  "module_column": "Workstream",
  "people_columns": [
    {{"label": "Owner", "header": "Owner"}},
    {{"label": "Reviewer", "header": "Reviewer "}}
  ],
  "assignee_column": "Owner",
  "effort_columns": [
    {{"label": "Effort", "header": "Effort (d)", "unit": "days"}}
  ],
  "description_column": "Summary",
  "type_column": "Category",
  "date_columns": {{
    "due": "Target Date",
    "go_live": null,
    "signoff": null,
    "start": "Raised On",
    "completion": null
  }},
  "critical_fields": ["Ticket Ref", "Workstream", "Category", "Summary", "Current State"]
}}

Return ONLY a valid JSON object with those keys."""


async def detect_schema_config(
    tab_name: str,
    raw_rows: List[List[str]],
    tab_names: List[str],
    client: AsyncOpenAI
) -> Dict[str, Any]:
    """
    Calls DeepSeek V3 to automatically analyze raw sheet rows
    to determine tracker validity, header location, and semantic role mapping.
    """
    if not raw_rows:
        return {"is_tracker_sheet": False}

    raw_rows_json = json.dumps(raw_rows, ensure_ascii=False)
    tab_names_json = json.dumps(tab_names, ensure_ascii=False)

    try:
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "user",
                "content": SCHEMA_DETECTION_PROMPT.format(
                    tab_name=tab_name,
                    raw_rows_json=raw_rows_json,
                    tab_names_json=tab_names_json
                )
            }],
            max_tokens=2048,
            temperature=0.1
        )
        content = response.choices[0].message.content.strip()
        
        result = _parse_json_safely(content)
        if result:
            return _normalise_people_and_effort(result)

    except Exception as e:
        logger.error(f"Error during schema auto-detection for tab '{tab_name}': {e}")

    # Structural fallback if LLM request fails — derived from this sheet's own header row.
    return _normalise_people_and_effort(_structural_fallback(raw_rows))


def _normalise_people_and_effort(tab_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Give people_columns/effort_columns a well-formed shape before they are stored.

    The model is asked for `label` and `header` but not `key`, and it may omit
    people_columns entirely while still returning the older `assignee_column`. Doing the
    repair once here means the persisted schema_config is always canonical, so neither
    the read path nor the admin editor has to defend against a half-populated config.
    The two directions are kept in sync both ways: a legacy `assignee_column` seeds the
    list, and a detected list re-derives `assignee_column` from its first entry so tools
    still reading the old key address the same column.
    """
    people = []
    seen_headers = set()
    for entry in tab_schema.get("people_columns") or []:
        if not isinstance(entry, dict):
            continue
        header = entry.get("header")
        if not header or header in seen_headers:
            continue
        seen_headers.add(header)
        label = str(entry.get("label") or header).strip()
        people.append({"key": slugify_role(label), "label": label, "header": header})

    legacy = tab_schema.get("assignee_column")
    if not people and legacy:
        label = str(legacy).strip()
        people.append({"key": slugify_role(label), "label": label, "header": legacy})

    effort = []
    seen_effort = set()
    for entry in tab_schema.get("effort_columns") or []:
        if not isinstance(entry, dict):
            continue
        header = entry.get("header")
        if not header or header in seen_effort:
            continue
        seen_effort.add(header)
        label = str(entry.get("label") or header).strip()
        effort.append({
            "key": slugify_role(label),
            "label": label,
            "header": header,
            "unit": entry.get("unit") or "days",
        })

    tab_schema["people_columns"] = people
    tab_schema["effort_columns"] = effort
    # Keep the legacy single-slot key pointing at the first role so anything still
    # reading assignee_column resolves to a column that exists.
    tab_schema["assignee_column"] = people[0]["header"] if people else None
    return tab_schema


def normalise_schema_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Canonicalise people/effort columns anywhere in a schema_config before it is stored.

    The admin UI can PUT a hand-edited schema_config, and the role editor sends whatever
    the admin assembled. Normalising on the way in makes the persisted config the single
    canonical shape, so the read path never has to defend against a half-populated entry
    written by hand. Handles both config shapes (multi-tab and flat) via the same
    "tabs" key test that schema.py:get_tab_schema uses.
    """
    if not isinstance(config, dict):
        return config
    if "tabs" in config and isinstance(config.get("tabs"), dict):
        config["tabs"] = {
            name: _normalise_people_and_effort(tab) if isinstance(tab, dict) else tab
            for name, tab in config["tabs"].items()
        }
        return config
    return _normalise_people_and_effort(config)


def _column_letter(index: int) -> str:
    """0-based column index -> A1 column letter (0 -> A, 26 -> AA)."""
    letter = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letter = chr(65 + rem) + letter
    return letter


def _match_header(headers: List[str], keywords: List[str]) -> Optional[str]:
    """First header containing any keyword (case-insensitive), else None."""
    for header in headers:
        lowered = header.lower()
        if any(kw in lowered for kw in keywords):
            return header
    return None


def _match_all_headers(headers: List[str], keywords: List[str]) -> List[str]:
    """Every header containing any keyword, in sheet order.

    The people/effort equivalents of _match_header: a sheet with both a technical and a
    functional resource column needs both, and taking only the first is the bug this
    whole change exists to fix.
    """
    return [h for h in headers if any(kw in h.lower() for kw in keywords)]


def _structural_fallback(raw_rows: List[List[Any]]) -> Dict[str, Any]:
    """Best-effort schema from the sheet's actual headers, used when LLM detection fails.

    This previously returned a hardcoded copy of one customer's WRICEF tracker — column
    names like "RICEFW ID" and "Technical Resource ", SAP module codes, and WRICEF type
    letters. On any other sheet every one of those columns is absent, so reads and writes
    silently addressed columns that do not exist, and the invented `valid_modules` became
    an allowlist that rejected legitimate IDs. Guessing from the real header row is worse
    than a correct LLM detection but strictly better than describing a different sheet.

    Deliberately omits `valid_modules`/`valid_types`: an unverified vocabulary is a
    constraint the model will enforce, and no vocabulary at all is the honest default.
    """
    rows = raw_rows or []
    # Header row = the earliest row with the most non-empty cells. Trackers routinely put
    # a title or blank spacer above the real header, which is why this isn't just row 0.
    header_idx = 0
    best_filled = -1
    for idx, row in enumerate(rows[:10]):
        filled = sum(1 for cell in row if str(cell).strip())
        if filled > best_filled:
            best_filled = filled
            header_idx = idx

    raw_headers = [str(c) for c in rows[header_idx]] if header_idx < len(rows) else []
    headers = [h.strip() for h in raw_headers]
    non_empty = [h for h in headers if h]
    # Stripped header -> the header exactly as the sheet spells it. people_columns must
    # carry the verbatim form ("Technical Resource " keeps its trailing space) because
    # that string is what addresses the column; the stripped form is only for display.
    verbatim = {h.strip(): h for h in raw_headers if h.strip()}

    primary_id_column = non_empty[0] if non_empty else ""
    primary_id_position = _column_letter(headers.index(primary_id_column)) if primary_id_column else "A"

    status_column = _match_header(non_empty, ["status", "state", "progress", "stage"])
    module_column = _match_header(non_empty, ["module", "workstream", "area", "team", "department", "function"])
    type_column = _match_header(non_empty, ["type", "category", "kind", "class"])
    people_headers = _match_all_headers(
        non_empty,
        ["assign", "owner", "resource", "responsible", "developer", "engineer", "consultant", "lead", "analyst"],
    )
    people_columns = [
        {"key": slugify_role(h), "label": h, "header": verbatim.get(h, h)}
        for h in people_headers
    ]
    assignee_column = people_columns[0]["header"] if people_columns else None

    effort_headers = _match_all_headers(
        non_empty,
        ["effort", "man-day", "man day", "mandays", "duration", "estimate", "est.", "story point", "days"],
    )
    effort_columns = [
        {"key": slugify_role(h), "label": h, "header": verbatim.get(h, h), "unit": "days"}
        for h in effort_headers
    ]

    description_column = _match_header(non_empty, ["description", "desc", "summary", "title", "subject"])

    # "due" is matched before "completion" and on stricter words. A header like
    # "Expected Completetion Date" is a deadline, not a record of finished work, but it
    # contains "completion" — so were completion matched first it would claim the column
    # and the sheet would end up with no deadline at all.
    date_columns = {
        "due": _match_header(non_empty, ["due", "deadline", "expected", "planned", "target", "eta"]),
        "go_live": _match_header(non_empty, ["go-live", "go live", "golive"]),
        "signoff": _match_header(non_empty, ["sign-off", "sign off", "signoff", "approved", "approval"]),
        "start": _match_header(non_empty, ["start", "created", "opened", "assigned"]),
        "completion": _match_header(non_empty, ["completion", "completed", "closed", "finished", "end date"]),
    }
    # The same header cannot be both the deadline and the actual completion date.
    if date_columns["due"] and date_columns["completion"] == date_columns["due"]:
        date_columns["completion"] = None

    critical = [c for c in (
        primary_id_column, module_column, type_column,
        description_column, status_column, assignee_column
    ) if c]

    return {
        "is_tracker_sheet": True,
        "header_row_index": header_idx,
        "primary_id_column": primary_id_column,
        "primary_id_position": primary_id_position,
        "status_column": status_column,
        "module_column": module_column,
        "people_columns": people_columns,
        "effort_columns": effort_columns,
        "assignee_column": assignee_column,
        "description_column": description_column,
        "type_column": type_column,
        "date_columns": {k: v for k, v in date_columns.items() if v},
        "critical_fields": critical,
    }


def _parse_json_safely(raw: str) -> Optional[dict]:
    """Parse JSON and strip any markdown fences if present."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None
