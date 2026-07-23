import json
import logging
import re
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI

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
    valid_modules = []
    
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
            consolidated_config[tab] = tab_schema
            if tab not in valid_modules:
                valid_modules.append(tab)
                
        except Exception as te:
            logger.warning(f"Error auto-detecting schema for tab '{tab}': {te}")
            continue
            
    return {
        "tabs": consolidated_config,
        "global": {
            "valid_modules": valid_modules,
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
   - "assignee_column": Header for responsible owner (e.g., Assignee, Technical Resource, Resource, Owner, Lead). Set to null if missing.
   - "description_column": Header for details/description (e.g., Description, Task Name, Summary, Details). Set to null if missing.
   - "type_column": Header for category/type (e.g., Type, RICEFW Type, Object Type, Category). Set to null if missing.
   - "date_columns": Object mapping "go_live", "signoff", "start", "completion" to their respective headers, or null if missing.

Return ONLY a valid JSON object matching this exact structure:
{{
  "is_tracker_sheet": true,
  "header_row_index": 0,
  "primary_id_column": "RICEFW ID",
  "primary_id_position": "B",
  "status_column": "Dev Status",
  "module_column": "Module",
  "assignee_column": "Technical Resource",
  "description_column": "Description",
  "type_column": "Type",
  "date_columns": {{
    "go_live": "Go-Live Date",
    "signoff": null,
    "start": "Start Date",
    "completion": null
  }},
  "critical_fields": ["RICEFW ID", "Module", "Type", "Description", "Dev Status"],
  "valid_modules": ["FI", "MM", "SD"],
  "valid_types": ["R", "I", "C", "E", "F", "W"]
}}
"""


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
            return result
        
    except Exception as e:
        logger.error(f"Error during schema auto-detection for tab '{tab_name}': {e}")

    # Structural fallback if LLM request fails
    return {
        "is_tracker_sheet": True,
        "header_row_index": 1,
        "primary_id_column": "RICEFW ID",
        "primary_id_position": "B",
        "status_column": "Dev Status",
        "module_column": "Module",
        "assignee_column": "Technical Resource ",
        "description_column": "Description",
        "type_column": "Type",
        "date_columns": {
            "go_live": "Go-Live Date",
            "signoff": "Sign-Off Date",
            "start": "Start Date",
            "completion": "Completion Date"
        },
        "critical_fields": ["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Technical Resource "],
        "valid_modules": ["FI", "MM", "SD", "PM", "QM", "PP", "TRM", "HCM", "IM", "CO", "FM", "PS"],
        "valid_types": ["R", "I", "C", "E", "F", "W"]
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
