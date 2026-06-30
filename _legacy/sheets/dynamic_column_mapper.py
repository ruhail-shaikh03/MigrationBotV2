"""
src/sheets/dynamic_column_mapper.py
────────────────────────────────────
F11 — Dynamic LLM-Driven Column Mapping

Analyses a sheet's actual header row and builds a natural-language alias map
using a two-pass DeepSeek call. The result is stored in
st.session_state["column_map"] and st.session_state["column_maps"] and used
everywhere COLUMN_ALIASES was used.
"""

import json
import streamlit as st
from typing import Dict, List, Any, Optional
from src.llm.deepseek_client import get_deepseek_client
from src.sheets.column_map import COLUMN_ALIASES

# ── Prompts ───────────────────────────────────────────────────────────────────

_PASS1_PROMPT = """\
You are building a natural-language alias map for a Google Sheet used to \
track SAP S/4HANA migration objects (a WRICEF tracker).

Below is the EXACT list of column headers from the sheet as a JSON array. \
Each header is reproduced verbatim — including any trailing spaces or typos \
— because the exact string is used for API calls.

Headers:
{headers_json}

Return a JSON object where:
- Each KEY is one of the exact header strings from the list above (copy verbatim)
- Each VALUE is an array of 3-6 lowercase strings a business user might say \
to refer to that column in plain English or SAP terminology

Rules:
- Do NOT add keys that are not in the headers list above
- Do NOT modify or trim the key strings in any way
- Do NOT wrap the response in markdown code blocks
- Focus on SAP/migration domain terms where relevant \
(e.g. BADI, enhancement exit, Z-table, tcode, RICEF, transport request)
- For headers with typos (e.g. "Functinal Resource ") still produce \
meaningful aliases — the typo is intentional to match the sheet
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
2. Aliases that are so generic they could match multiple columns \
(e.g. "type" appearing for both "Type" and "Enhancement Type") — make them \
more specific for the ambiguous column
3. Obvious missing SAP terms \
(e.g. "badi", "user exit", "tcode", "z table", "abaper", "transport", "golive")

Return a corrected JSON object with the same key/value structure.
If nothing needs changing, return the original map unchanged.
Return ONLY valid JSON, no explanation, no markdown code blocks.\
"""


# ── Core analysis ─────────────────────────────────────────────────────────────

def build_column_map(header_row: List[str]) -> dict:
    """
    Run the two-pass LLM analysis on a header row and return the alias dict.
    Keys are exact header strings; values are lists of lowercase alias strings.

    Falls back to COLUMN_ALIASES if any step fails.
    """
    if not header_row:
        return COLUMN_ALIASES

    client = get_deepseek_client()
    headers_json = json.dumps(header_row, ensure_ascii=False)

    # ── Pass 1: generate aliases ──────────────────────────────────────────────
    try:
        r1 = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user",
                        "content": _PASS1_PROMPT.format(headers_json=headers_json)}],
            max_tokens=4096,
            temperature=0.2,
        )
        raw1 = r1.choices[0].message.content.strip()
        map1 = _parse_json_safely(raw1)
    except Exception as e:
        st.warning(f"Column map generation failed (using static fallback): {e}")
        return COLUMN_ALIASES

    if not map1:
        st.warning("Column map generation returned invalid JSON — using static fallback.")
        return COLUMN_ALIASES

    # Sanity check: remove any hallucinated keys not in the actual header row
    header_set = set(header_row)
    map1 = {k: v for k, v in map1.items() if k in header_set}

    # ── Pass 2: verify and correct ────────────────────────────────────────────
    try:
        r2 = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user",
                        "content": _PASS2_PROMPT.format(
                            headers_json=headers_json,
                            map_json=json.dumps(map1, indent=2, ensure_ascii=False),
                        )}],
            max_tokens=4096,
            temperature=0.1,
        )
        raw2 = r2.choices[0].message.content.strip()
        map2 = _parse_json_safely(raw2)
    except Exception:
        # Pass 2 failure is non-fatal — use the pass 1 result
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
    """
    Parse a JSON string that may have stray markdown fences.
    Returns None on any parse failure.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(lines[1:-1]).strip()
    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


# ── Session-level map management ──────────────────────────────────────────────

def get_active_column_map() -> dict:
    """
    Return the active column map.
    Uses the session-level LLM-generated map if available,
    falls back to the static COLUMN_ALIASES otherwise.
    """
    return st.session_state.get("column_map", COLUMN_ALIASES)


def invalidate_column_map() -> None:
    """
    Remove the cached column map from session state.
    Called when the user switches sheets or tabs, so the next render
    triggers a fresh analysis.
    """
    st.session_state.pop("column_map", None)
    st.session_state.pop("column_map_sheet_key", None)
    st.session_state.pop("column_maps", None)


def ensure_column_map(executor: Any) -> None:
    """
    Build the column map for the current sheet and tab if it hasn't been built yet,
    or if the active sheet/tab has changed since it was last built.

    Call this once per render cycle, after the executor is initialised.
    Shows a spinner during the ~3-6 second LLM analysis.
    """
    current_key = f"{executor.spreadsheet_id}:{executor.SHEET_NAME}"

    if "column_maps" not in st.session_state:
        st.session_state["column_maps"] = {}

    # Check multi-tab column map cache first
    if current_key in st.session_state["column_maps"]:
        st.session_state["column_map"] = st.session_state["column_maps"][current_key]
        st.session_state["column_map_sheet_key"] = current_key
        return

    with st.spinner("Analysing sheet columns… (first load only)"):
        header_row = executor._get_header_row()
        column_map = build_column_map(header_row)

    st.session_state["column_maps"][current_key] = column_map
    st.session_state["column_map"] = column_map
    st.session_state["column_map_sheet_key"] = current_key