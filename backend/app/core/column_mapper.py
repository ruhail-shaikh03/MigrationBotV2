import json
import logging
from difflib import get_close_matches
from typing import List, Dict, Optional, Any
from openai import AsyncOpenAI

logger = logging.getLogger("column_mapper")

# Static aliases fallback map
COLUMN_ALIASES: Dict[str, List[str]] = {
    # --- Core Object Details ---
    "Module":                        ["module", "sap module", "functional area"],
    "RICEFW ID":                     ["ricefw id", "id", "object id", "ricefw", "primary key"],
    "Type":                          ["type", "object type", "ricefw type"],
    "Description":                   ["description", "desc", "object description"],
    "Source (ECC/S4)":               ["source", "system source", "ecc/s4", "origin"],
    "Business Owner":                ["business owner", "owner", "business contact", "stakeholder"],
    "Company Codes":                 ["company codes", "company code", "bukrs", "cocd"],
    "Actively Used?":                ["actively used", "is used", "in use", "currently used"],
    "Last Used Date":                ["last used date", "last used", "date last used"],
    
    # --- Migration Decision & Logic ---
    "Migrate?":                      ["migrate", "migration decision", "should migrate", "migrate?"],
    "Logic Change?":                 ["logic change", "logic changed", "requires logic change"],
    "Logic Change Details":          ["logic change details", "change details", "logic comments"],
    "Std SAP Available?":            ["std sap available", "standard available", "sap standard available"],
    "Copy of Std?":                  ["copy of std", "copy of standard", "z copy", "standard copy"],
    "Std Ref":                       ["std ref", "standard reference", "reference object"],
    
    # --- Enhancement Controls ---
    "Enhancement Used?":             ["enhancement used", "is enhanced", "has enhancement"],
    "Enhancement Type (Exit/BADI/etc)": ["enhancement type", "exit type", "badi", "user exit"],
    "Enhancement Replaced by Std?":  ["enhancement replaced", "replaced by std", "standard replacement"],
    "Hard Coding Present?":          ["hard coding", "hardcoded", "hard-coded values", "hardcoding"],
    "Z Tables Used?":                ["z tables used", "custom tables used", "uses z tables"],
    "Z Tables Req?":                 ["z tables req", "ztable needed", "custom tables required"],
    "Custom Approval Logic?":        ["custom approval", "approval logic", "custom workflow"],
    "Flexible Workflow Eval?":       ["flexible workflow", "workflow eval", "flex workflow"],
    
    # --- Forms & Output ---
    "SmartForm?":                    ["smartform", "smart form", "has smartform"],
    "Adobe Conversion Req?":         ["adobe", "adobe conversion", "pdf conversion", "adobe forms"],
    "Layout Change Req?":            ["layout change", "layout change req", "needs layout change"],
    
    # --- Batch & Automation ---
    "Background Job?":               ["background job", "batch job", "runs in background"],
    "Job Frequency":                 ["frequency", "job frequency", "batch schedule", "schedule"],
    "Job Dependency?":               ["job dependency", "batch dependency", "job dependencies"],
    "Scheduling Change Req?":        ["scheduling change", "schedule change", "needs new schedule"],
    
    # --- Data, Volume & Performance ---
    "High Data Volume?":             ["high data volume", "hdv", "large data", "volume"],
    "Data Model Impact (S4)?":       ["data model impact", "s4 data model", "data impact"],
    "Data Migration Dependency?":    ["data migration dependency", "data dependency"],
    "Performance OK?":               ["performance ok", "perf ok", "good performance"],
    "Performance Optimization Req?": ["performance optimization", "needs optimization", "tuning required"],
    "Report Redesign Req?":          ["report redesign", "redesign needed", "needs redesign"],
    "CDS Recommended?":              ["cds", "cds view", "needs cds", "pushdown"],
    "Dependent Objects":             ["dependent objects", "dependencies", "related objects"],
    
    # --- Project Management & Status ---
    "UAT Required?":                 ["uat required", "needs uat", "uat"],
    "Functional Sign-Off":           ["functional sign-off", "func signoff", "functional approval"],
    "Technical Review":              ["technical review", "tech review", "code review"],
    "Sign-Off Date":                 ["sign-off date", "signoff date", "approved date"],
    "Additional Remarks":            ["additional remarks", "remarks", "notes", "comments"],
    "Z-Tcode":                       ["z-tcode", "tcode", "transaction code", "custom tcode"],
    
    # Typos/spaces from default sheets
    "Technical Resource ":           ["technical resource", "technical owner", "tech resource", "developer", "abaper"],
    "Functinal Resource ":           ["functional resource", "functional owner", "func resource", "business analyst", "functional"],
    "Start Date":                    ["start date", "dev start date", "kickoff date"],
    "Programe Name":                 ["program name", "programe name", "object name", "report name"],
    "Dev Status":                    ["status", "dev status", "development status", "progress"],
    "Technical Remarks":             ["technical remarks", "tech remarks", "tech notes", "developer comments"],
    "Color ":                        ["color", "flag", "highlight flag", "priority flag", "marker"]
}

_PASS1_PROMPT = """\
You are building a natural-language alias map for a Google Sheet used to track SAP S/4HANA migration objects (a WRICEF tracker).

Below is the EXACT list of column headers from the sheet as a JSON array. Each header is reproduced verbatim — including any trailing spaces or typos — because the exact string is used for API calls.

Headers:
{headers_json}

Return a JSON object where:
- Each KEY is one of the exact header strings from the list above (copy verbatim)
- Each VALUE is an array of 3-6 lowercase strings a business user might say to refer to that column in plain English or SAP terminology

Rules:
- Do NOT add keys that are not in the headers list above
- Do NOT modify or trim the key strings in any way
- Do NOT wrap the response in markdown code blocks
- Focus on SAP/migration domain terms where relevant (e.g. BADI, enhancement exit, Z-table, tcode, RICEF, transport request)
- For headers with typos (e.g. "Functinal Resource ") still produce meaningful aliases — the typo is intentional to match the sheet
- Return ONLY valid JSON, nothing else\
"""

_PASS2_PROMPT = """\
Review this column alias map for a WRICEF migration tracker Google Sheet.

Original headers (exact, including spaces and typos):
{headers_json}

Generated alias map:
{map_json}

Check for ALL of the following:
1. Headers from the original list that have no entry in the map — add them
2. Aliases that are so generic they could match multiple columns (e.g. "type" appearing for both "Type" and "Enhancement Type") — make them more specific for the ambiguous column
3. Obvious missing SAP terms (e.g. "badi", "user exit", "tcode", "z table", "abaper", "transport", "golive")

Return a corrected JSON object with the same key/value structure.
If nothing needs changing, return the original map unchanged.
Return ONLY valid JSON, no explanation, no markdown code blocks.\
"""


def resolve_column(user_term: str, column_map: Optional[dict] = None) -> Optional[str]:
    """
    Resolve a natural-language field reference to a canonical column name.
    Resolution order:
      1. Exact match against canonical keys (case-insensitive, stripped)
      2. Alias list match
      3. Fuzzy match via difflib (cutoff 0.6)
    """
    active_map = column_map or COLUMN_ALIASES
    term = user_term.lower().strip()

    # 1 & 2. Exact match & alias match
    for canonical, aliases in active_map.items():
        if term == canonical.lower().strip() or term in [a.lower().strip() for a in aliases]:
            return canonical

    # 3. Fuzzy match
    match = get_close_matches(term, [c.lower().strip() for c in active_map], n=1, cutoff=0.6)
    if match:
        return next(c for c in active_map if c.lower().strip() == match[0])
    return None


def get_column_map_json(column_map: Optional[dict] = None) -> str:
    """
    Return the active column map as indented JSON for system prompt injection.
    """
    active_map = column_map or COLUMN_ALIASES
    return json.dumps(active_map, indent=2)


async def build_column_map(header_row: List[str], client: AsyncOpenAI) -> dict:
    """
    Run the two-pass LLM analysis on a header row and return the alias dictionary.
    Falls back to COLUMN_ALIASES if any step fails.
    """
    if not header_row:
        return COLUMN_ALIASES

    headers_json = json.dumps(header_row, ensure_ascii=False)

    # --- Pass 1: generate aliases ---
    try:
        r1 = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": _PASS1_PROMPT.format(headers_json=headers_json)}],
            max_tokens=4096,
            temperature=0.2,
        )
        raw1 = r1.choices[0].message.content.strip()
        map1 = _parse_json_safely(raw1)
    except Exception as e:
        logger.warning(f"Column map generation Pass 1 failed (using static fallback): {e}")
        return COLUMN_ALIASES

    if not map1:
        logger.warning("Column map generation returned invalid JSON in Pass 1 — using static fallback.")
        return COLUMN_ALIASES

    # Sanity check: remove any hallucinated keys not in the actual header row
    header_set = set(header_row)
    map1 = {k: v for k, v in map1.items() if k in header_set}

    # --- Pass 2: verify and correct ---
    try:
        r2 = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": _PASS2_PROMPT.format(
                headers_json=headers_json,
                map_json=json.dumps(map1, indent=2, ensure_ascii=False),
            )}],
            max_tokens=4096,
            temperature=0.1,
        )
        raw2 = r2.choices[0].message.content.strip()
        map2 = _parse_json_safely(raw2)
    except Exception as e:
        logger.warning(f"Column map generation Pass 2 failed (using Pass 1 result): {e}")
        map2 = None

    final_map = map2 if map2 else map1

    # Final sanity check: remove hallucinated keys again after pass 2
    final_map = {k: v for k, v in final_map.items() if k in header_set}

    # Ensure every header has at least an empty alias list
    for h in header_row:
        if h not in final_map:
            final_map[h] = []

    return final_map


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
