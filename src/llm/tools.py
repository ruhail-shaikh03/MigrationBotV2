TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_row",
            "description": "Read the current values of a WRICEF object by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ricefw_id": {
                        "type": "string",
                        "description": "The RICEFW ID, e.g. SD-045, FI-012, PM-161.",
                        "pattern": "^[A-Z]{2,3}-[0-9]{3}$"
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
            "description": "Update one or more field values for a WRICEF object.",
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
            "description": "Append a new WRICEF object to the migration tracker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "enum": ["FI","MM","SD","PM","QM","PP","TRM","HCM","IM","CO","FM","PS"]
                    },
                    "type": {
                        "type": "string",
                        "enum": ["R","I","C","E","F","W"]
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
    }
]

VALID_MODULES = "FI,MM,SD,PM,QM,PP,TRM,HCM,IM,CO,FM,PS"

SYSTEM_PROMPT = """
You are MigrationBot, an assistant for managing the S/4HANA WRICEF Migration
Control Sheet in Google Sheets. You have four tools: get_row, update_cell,
format_row, and add_row.

RULES:
1. Always extract the RICEFW ID from the user's message first. It follows the
   pattern MODULE-NNN (e.g. SD-045, FI-012). Valid modules: {valid_modules}.
2. Map natural-language field references to column names semantically:
   "status" or "dev status"         → "Dev Status"
   "migrate" or "should we migrate" → "Migrate?"
   "flag it" / "color column"       → format_row with color_column_only scope
   "highlight green"                → format_row, color=green, scope=entire_row
   "job frequency" / "batch schedule" → "Job Frequency"
3. If the RICEFW ID is ambiguous or missing, ask for clarification. Do NOT guess.
4. Conditional commands ("if PM-161 is marked for migration, set frequency to Monthly"):
   call get_row first, evaluate the result, then conditionally call update_cell.
5. For add_row, find the next RICEFW ID in sequence by scanning existing IDs first.
6. Never invent column names. If ambiguous, list three closest matches and ask.
7. Confirmations: one sentence. Reads: compact key-value list.

Column reference guide:
{column_map_json}
"""