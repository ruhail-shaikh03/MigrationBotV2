from typing import List, Dict, Any

# Tool schemas available to DeepSeek in OpenAI function calling format.
#
# These describe a generic row-tracking spreadsheet, NOT specifically an SAP WRICEF
# tracker. Identifier shapes, category names and column names differ per sheet and come
# from the project's detected schema_config (core/schema_detect.py) and column map
# (core/column_mapper.py) — never from literals here. A `pattern` or `enum` in these
# schemas is a hard constraint the model physically cannot emit around, so encoding one
# customer's taxonomy here silently makes every other sheet unusable: `SLCM-0586` was
# rejected outright by both the get_row id pattern and the module enums below.
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_row",
            "description": "Read the current values of a single row by its unique ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ricefw_id": {
                        "type": "string",
                        "description": (
                            "The row's unique identifier, exactly as it appears in this "
                            "sheet's primary ID column — e.g. SD-045, FFC-SD-045, "
                            "SLCM-0586, TASK-1194, INC0042213. Pass it through verbatim; "
                            "do not reformat it or assume a particular prefix or length."
                        )
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific field names to return. Empty = return all."
                    }
                },
                "required": ["ricefw_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_cell",
            "description": "Update one or more field values on a single row.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ricefw_id": {"type": "string"},
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field":  {"type": "string"},
                                "value": {"type": "string"}
                              },
                            "required": ["field", "value"]
                        }
                    }
                },
                "required": ["ricefw_id", "updates"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "format_row",
            "description": "Apply background color to a row or specific cells.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ricefw_id": {"type": "string"},
                    "color": {
                        "type": "string",
                        "enum": ["red", "green", "amber", "blue", "white"]
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["entire_row", "color_column_only"],
                        "default": "color_column_only"
                    }
                },
                "required": ["ricefw_id", "color"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_row",
            "description": "Append a new row to the active sheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "description": (
                            "The row's category, as this sheet expresses it — whatever "
                            "value belongs in its category/module column (e.g. SD, SLCM, "
                            "Finance, Onboarding). Use a value consistent with existing "
                            "rows; do not translate it into some other vocabulary."
                        )
                    },
                    "type": {
                        "type": "string",
                        "description": (
                            "The row's type/classification as this sheet expresses it. "
                            "Match the convention already used in the sheet's type column."
                        )
                    },
                    "description": {"type": "string"},
                    "assigned_to":  {"type": "string"},
                    "fields": {
                        "type": "object",
                        "additionalProperties": {"type": "string"}
                    }
                },
                "required": ["module", "type", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_update",
            "description": (
                "Update one field to one value across multiple rows at once. "
                "Use this when the user says things like 'mark all of these as done', "
                "'set everyone on this list to Ready for Dev', or "
                "'update SD-001 through SD-005 status to In Progress'. "
                "Can also accept a filter (e.g. category + current field value) instead "
                "of an explicit list of IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ricefw_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Explicit list of row IDs to update, verbatim as they appear "
                            "in the sheet. Provide either this OR filter_by, not both."
                        )
                    },
                    "filter_by": {
                        "type": "object",
                        "description": (
                            "Instead of listing IDs, describe which rows to target, using "
                            "this sheet's own column names and values. Shape (illustrative "
                            "only, not real column names): {\"module\": \"<category>\", "
                            "\"field\": \"<column>\", \"value\": \"<current value>\"}. "
                            "All three sub-keys are required if filter_by is used."
                        ),
                        "properties": {
                            "module": {
                                "type": "string",
                                "description": (
                                    "Value to match in this sheet's category/module "
                                    "column, using the sheet's own vocabulary."
                                )
                            },
                            "field": {
                                "type": "string",
                                "description": "Column name to filter on."
                            },
                            "value": {
                                "type": "string",
                                "description": "Current cell value to match."
                            }
                        }
                    },
                    "set_field": {
                        "type": "string",
                        "description": "The column to update on every matched row."
                    },
                    "set_value": {
                        "type": "string",
                        "description": "The value to write into set_field."
                    }
                },
                "required": ["set_field", "set_value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_rows",
            "description": (
                "Search the active sheet and return all rows that match one or more "
                "field criteria. Supports single-field and multi-field filters. Use this "
                "when the user asks things like 'show me all SD items owned by Ahmed', "
                "'which items are still open?', 'find everything with no status', or "
                "'list everything assigned to Sara'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "array",
                        "description": (
                            "List of field/value pairs that all must match "
                            "(AND logic). Use an empty string value to find "
                            "rows where that field is blank."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {
                                    "type": "string",
                                    "description": "Column name to filter on."
                                },
                                "value": {
                                    "type": "string",
                                    "description": (
                                        "Value to match. Case-insensitive. "
                                        "Use empty string \"\" to find blank cells."
                                    )
                                },
                                "match_type": {
                                    "type": "string",
                                    "enum": ["exact", "contains", "blank"],
                                    "default": "exact",
                                    "description": (
                                        "'exact': cell equals value. "
                                        "'contains': cell contains value as substring. "
                                        "'blank': cell is empty (value is ignored)."
                                    )
                                }
                            },
                            "required": ["field", "value"]
                        },
                        "minItems": 1
                    },
                    "return_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Which columns to include in each result row, using this "
                            "sheet's own column names. If omitted, returns the sheet's "
                            "key columns (id, category, type, description, status, "
                            "assignee) as resolved from its detected schema."
                        )
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default 20.",
                        "default": 20
                    }
                },
                "required": ["filters"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "summarize",
            "description": (
                "Aggregate and count data across the active sheet. Use this for "
                "questions like 'how many items are in each status?', 'what's our "
                "overall completion rate?', 'how many items are assigned to each "
                "person?', 'which items have no status?', or "
                "'how many are past their due date?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": [
                            "count_by_field",
                            "completion_rate",
                            "blank_fields",
                            "overdue"
                        ],
                        "description": (
                            "'count_by_field': group rows by a field's values and count each group. "
                            "'completion_rate': what % of rows have a specific field set to a "
                            "target value. "
                            "'blank_fields': count rows where a field is empty. "
                            "'overdue': rows whose due/target date column is in the past and "
                            "whose status column is not a completion value."
                        )
                    },
                    "group_by_field": {
                        "type": "string",
                        "description": (
                            "Required for count_by_field. The column to group rows by, "
                            "named as it appears in this sheet — consult the column "
                            "reference guide rather than guessing a conventional name."
                        )
                    },
                    "scope_module": {
                        "type": "string",
                        "description": (
                            "Optional. Restrict the report to one value of this sheet's "
                            "category/module column, using the sheet's own vocabulary. "
                            "Omit to report across every row."
                        )
                    },
                    "completion_field": {
                        "type": "string",
                        "description": (
                            "Required for completion_rate. The column to measure, named "
                            "as it appears in this sheet."
                        )
                    },
                    "completion_value": {
                        "type": "string",
                        "description": (
                            "Required for completion_rate. The value that counts as "
                            "'complete'. E.g. 'Yes', 'Done', 'Ready for Dev'."
                        )
                    },
                    "blank_field": {
                        "type": "string",
                        "description": "Required for blank_fields. Column to check for blanks."
                    },
                    "overdue_status_exclusions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For overdue report: status values that mean 'done' and should "
                            "be excluded, in this sheet's own wording. Defaults to "
                            "['Complete', 'Done', 'Closed', 'Go-Live', 'Retired']."
                        )
                    }
                },
                "required": ["report_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_module",
            "description": (
                "Switch the active tab inside the current spreadsheet. Use this when the "
                "requested data lives on a different tab than the active one. Only call it "
                "for a tab named in the available-tabs list; many sheets keep everything on "
                "a single tab, in which case this tool is never needed. A row ID whose "
                "prefix is unfamiliar is NOT evidence that a different tab is required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_name": {
                        "type": "string",
                        "description": "The exact tab name to switch to, from the available-tabs list."
                    }
                },
                "required": ["tab_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "data_quality",
            "description": (
                "Run data-quality and validation checks on the active sheet. "
                "Use this when the user asks about data health, anomalies, completeness, "
                "inconsistencies, or validation issues — e.g. 'check data quality', "
                "'which items have no assigned dev?', 'find rows missing required fields', "
                "'show me blank cells', 'any items completed but lacking a sign-off date?', "
                "'are there stale items?', or 'what is our completeness score?'. "
                "Also use for 'flag any items past their due date' "
                "(maps to overdue check_type)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "check_type": {
                        "type": "string",
                        "enum": [
                            "blank_fields",
                            "consistency",
                            "stale",
                            "completeness_score",
                            "all"
                        ],
                        "description": (
                            "'blank_fields': count blank cells per critical column. "
                            "'consistency': detect logical anomalies (e.g. marked complete "
                            "but missing a sign-off/completion date). "
                            "'stale': items with no audit activity for threshold_days. "
                            "'completeness_score': 0-100 fill-rate across critical columns. "
                            "'all': run every check and return a combined report."
                        )
                    },
                    "scope_module": {
                        "type": "string",
                        "description": (
                            "Optional. Restrict checks to one value of this sheet's "
                            "category/module column, in the sheet's own vocabulary."
                        )
                    },
                    "threshold_days": {
                        "type": "integer",
                        "description": (
                            "For stale check only: number of days without audit activity "
                            "before an item is considered stale. Default 30."
                        ),
                        "default": 30
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "For blank_fields check only: specific column names to check. "
                            "If omitted, checks the standard critical columns."
                        )
                    }
                },
                "required": ["check_type"]
            }
        }
    },
]

# Base prompt templates
BASE_SYSTEM_PROMPT = """
You are MigrationBot, a highly capable assistant for managing tracking spreadsheets in
Google Sheets. Sheets vary widely between teams — SAP migration trackers, project and task
registers, issue logs, onboarding checklists. The active sheet's real structure is given by
the column reference guide at the end of this prompt; treat that as the only authority on
what exists. You have nine strict tools: get_row, update_cell,
format_row, add_row, bulk_update, search_rows, summarize, switch_module, and data_quality.

CRITICAL ANTI-PATTERNS (NEVER DO THESE):
- NEVER call `get_row` iteratively in a loop for multiple items. `get_row` is strictly for reading a SINGLE specific row ID.
- NEVER try to fetch all rows to manually do math, aggregate, or search. The system will forcefully terminate you if you loop.
- ALWAYS use the macro-tools (`search_rows`, `summarize`, `bulk_update`, `data_quality`) for ANY operation involving more than one row.

RULES:
1. Always extract the row ID from the user's message first, and pass it through
   EXACTLY as written. Identifier formats differ per sheet — SD-045, FFC-SD-045,
   SLCM-0586, TASK-1194, INC0042213 are all legitimate. Never reject, rewrite, or
   question an ID because its prefix or length looks unfamiliar; you do not hold a
   list of valid prefixes, and the sheet is the authority. If the ID genuinely is
   not present, the tool will say so — let it.
2. Map natural-language field references to column names semantically, resolving
   against the column reference guide below rather than assuming conventional names:
   "status"                         → whichever column tracks status here
   "owner" / "who's on it"          → whichever column names a person
   "flag it" / "color column"       → format_row with color_column_only scope
   "highlight green"                → format_row, color=green, scope=entire_row
3. If the row ID is ambiguous or missing, ask for clarification. Do NOT guess.
4. Conditional commands ("if PM-161 is approved, set frequency to Monthly"):
   call get_row first, evaluate the result, then conditionally call update_cell.
5. For add_row, the row ID is assigned automatically server-side in sequence — never
   invent or guess one yourself, and do not ask the user for it.
6. Never invent column names. If ambiguous, list three closest matches and ask.
7. Confirmations: one sentence. Reads: compact key-value list.
8. BULK OPERATIONS — use bulk_update when the user provides a list of IDs or
   says "all [module] items where [condition]". Always confirm how many rows
   will be affected before summarising results. Use filter_by when the user
   describes a condition rather than listing IDs explicitly.
9. SEARCH — use search_rows when the user asks "show me", "find", "list", or
   "which items". If they don't specify return_fields, use the default set.
   For partial-name searches on people ("find Sara's items") use match_type=contains.
   For "items with no dev status" use match_type=blank.
10. REPORTING — use summarize when the user asks "how many", "what percentage",
    "completion rate", "overdue", or "which fields are empty". Always pick the
    most specific report_type. For "how complete is the SD workstream?" use
    completion_rate. For "who has the most items?" use count_by_field on the
    sheet's assignee column with scope_module omitted.
11. Never call bulk_update without confirming the target set of rows in your
    reply.
12. TABS — Available tabs: {available_tabs}. If the requested data lives on a
    different tab than the active one, call switch_module(tab_name) first, using an
    exact name from that list. Many sheets hold everything on one tab; if the list is
    empty or has a single entry, never call switch_module. An unfamiliar ID prefix is
    not a reason to switch tabs — prefixes are not tab names.
13. DATA QUALITY — use data_quality when the user asks about data health,
    missing values, anomalies, stale items, validation issues, or completeness.
    Use check_type="all" for general "check data quality" requests.
    Use check_type="blank_fields" for "what's missing" or "which fields are empty".
    Use check_type="consistency" for "any contradictions" or "logic errors".
    Use check_type="stale" for "items with no recent activity".
    Use check_type="completeness_score" for "how complete is our data?".
14. WRITES ARE ASYNCHRONOUS — update_cell, bulk_update, add_row, and format_row all
    return {{"status": "queued", ...}} the instant the request is accepted, before the
    change has actually reached the spreadsheet. NEVER say "Updated", "Done", "Set",
    or otherwise imply a write already succeeded. Phrase it as pending — e.g. "Queued
    an update for SD-045's status to Done." The user is notified separately once
    the write actually completes or fails; you do not report that outcome yourself.

Column reference guide:
{column_map_json}
"""

BASE_SYSTEM_PROMPT_COMPACT = """
You are MigrationBot managing a tracking spreadsheet in Google Sheets.
Available tabs: {available_tabs}.
You have nine tools: get_row, update_cell, format_row, add_row,
bulk_update, search_rows, summarize, switch_module, and data_quality.

CRITICAL REMINDER: NEVER call `get_row` iteratively for multiple items. Use `search_rows` or `summarize` for ANY multi-row query.
Row IDs and column names belong to this sheet's own vocabulary — pass IDs through verbatim and never reject one for looking unfamiliar.

The column reference guide is already present earlier in this conversation.
Continue the task. Follow all previous rules. Respond with tool calls or a
final summary — do NOT re-explain what you are doing.
"""


def _format_tabs(available_tabs: List[str]) -> str:
    """Render the available-tab list for prompt injection.

    Deliberately has no hardcoded fallback. Both builders used to substitute the literal
    "FI,MM,SD,PM,QM,PP,TRM,HCM,IM,CO,FM,PS" whenever the list was empty, and the prompt
    presented it as "Valid modules", so a single-tab sheet — which legitimately has no tabs
    to switch between — was told one customer's SAP module codes were the only legal
    vocabulary. That is what made the model refuse SLCM-0586. An empty list now says so
    plainly, which is both true and non-constraining.
    """
    return ",".join(available_tabs) if available_tabs else "(single tab — switch_module is not applicable)"


def get_system_prompt(available_tabs: List[str], column_map_json: str) -> str:
    """Format and return the main system prompt with available tabs and active column map."""
    return BASE_SYSTEM_PROMPT.format(
        available_tabs=_format_tabs(available_tabs),
        column_map_json=column_map_json
    )


def get_system_prompt_compact(available_tabs: List[str]) -> str:
    """Format and return the compact system prompt with available tabs."""
    return BASE_SYSTEM_PROMPT_COMPACT.format(available_tabs=_format_tabs(available_tabs))
