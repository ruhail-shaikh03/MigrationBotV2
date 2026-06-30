import json
import logging
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI

logger = logging.getLogger("schema_detect")

SCHEMA_DETECTION_PROMPT = """
You are analyzing a Google Sheet header row for a migration/project tracker.

Headers (exact, verbatim):
{headers_json}

Sample data (first 3 rows):
{sample_rows_json}

Tab names in this spreadsheet:
{tab_names_json}

Identify the semantic role of each column. Return JSON:
{{
  "primary_id_column": "<header containing the unique object ID, e.g. RICEFW ID>",
  "primary_id_position": "<column letter, e.g. B, matching the 1-based index of primary_id_column>",
  "status_column": "<header tracking dev/migration status, e.g. Dev Status>",
  "module_column": "<header for functional area/module/workstream, e.g. Module>",
  "assignee_column": "<header for person assigned, e.g. Technical Resource or Functional Resource>",
  "description_column": "<header for object description, e.g. Description>",
  "type_column": "<header for object type/category, e.g. Type>",
  "date_columns": {{
    "go_live": "<header for target/go-live date, or null>",
    "signoff": "<header for sign-off/approval date, or null>",
    "start": "<header for start date, or null>",
    "completion": "<header for completion date, or null>"
  }},
  "critical_fields": ["<top 5-6 essential headers for brief search displays>"],
  "valid_modules": ["<modules or functional codes parsed from tabs or sample column values, e.g. FI, MM, SD>"],
  "valid_types": ["<unique types parsed from the type column sample, e.g. R, I, C, E, F, W>"]
}}
Return ONLY valid JSON.
"""


async def detect_schema_config(
    headers: List[str],
    sample_rows: List[List[str]],
    tab_names: List[str],
    client: AsyncOpenAI
) -> Dict[str, Any]:
    """
    Calls DeepSeek V3 to automatically analyze sheet headers and sample data
    to produce the semantic role mapping required for sheets executor operations.
    """
    if not headers:
        return {}

    headers_json = json.dumps(headers, ensure_ascii=False)
    sample_rows_json = json.dumps(sample_rows, ensure_ascii=False)
    tab_names_json = json.dumps(tab_names, ensure_ascii=False)

    try:
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "user",
                "content": SCHEMA_DETECTION_PROMPT.format(
                    headers_json=headers_json,
                    sample_rows_json=sample_rows_json,
                    tab_names_json=tab_names_json
                )
            }],
            max_tokens=2048,
            temperature=0.1
        )
        content = response.choices[0].message.content.strip()
        
        # Safely parse JSON from LLM response (handling potential markdown wrapper)
        result = _parse_json_safely(content)
        if result:
            # Inject default data_start_row if not set
            if "data_start_row" not in result:
                result["data_start_row"] = 3 # Legacy standard default
            return result
        
    except Exception as e:
        logger.error(f"Error during schema auto-detection: {e}")

    # Return safe structural fallback defaults
    return {
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
        "valid_types": ["R", "I", "C", "E", "F", "W"],
        "data_start_row": 3
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
