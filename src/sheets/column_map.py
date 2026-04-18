import json
from difflib import get_close_matches

COLUMN_ALIASES = {
    "Migrate?":              ["migrate", "migration decision", "should migrate", "migrate?"],
    "Dev Status":            ["status", "dev status", "development status", "progress"],
    "Logic Change?":         ["logic change", "logic changed", "requires logic change"],
    "Hard Coding Present?":  ["hard coding", "hardcoded", "hard-coded values"],
    "CDS Recommended?":      ["cds", "cds view", "needs cds"],
    "Adobe Conversion Req?": ["adobe", "adobe conversion", "pdf conversion"],
    "Job Frequency":         ["frequency", "job frequency", "batch schedule", "schedule"],
    "Color":                 ["color", "flag", "highlight flag", "priority flag"],
    "Technical Resource":    ["technical owner", "tech resource", "developer"],
    "Z Tables Req?":         ["z table", "custom table", "ztable needed"],
    "Functional Resource":   ["functional owner", "func resource", "business analyst"],
    "Go-Live Date":          ["go live", "golive", "launch date", "live date"],
    "UAT Status":            ["uat", "user acceptance", "testing status"],
    "Transport Number":      ["transport", "tr number", "workbench request"],
    # ↑ Add all your columns here. Run _get_header_row() once and paste the
    # exact header strings to make sure spellings and spaces match.
}

def resolve_column(user_term: str) -> str | None:
    term = user_term.lower().strip()
    for canonical, aliases in COLUMN_ALIASES.items():
        if term == canonical.lower() or term in aliases:
            return canonical
    match = get_close_matches(term, [c.lower() for c in COLUMN_ALIASES], n=1, cutoff=0.6)
    if match:
        return next(c for c in COLUMN_ALIASES if c.lower() == match[0])
    return None

def get_column_map_json() -> str:
    return json.dumps(COLUMN_ALIASES, indent=2)