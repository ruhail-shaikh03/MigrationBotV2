import json
from difflib import get_close_matches

COLUMN_ALIASES = {
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
    
    # ⚠️ EXACT TYPOS/SPACES FROM YOUR SHEET ⚠️
    "Technical Resource ":           ["technical resource", "technical owner", "tech resource", "developer", "abaper"],
    "Functinal Resource ":           ["functional resource", "functional owner", "func resource", "business analyst", "functional"],
    "Start Date":                    ["start date", "dev start date", "kickoff date"],
    "Programe Name":                 ["program name", "programe name", "object name", "report name"],
    "Dev Status":                    ["status", "dev status", "development status", "progress"],
    "Technical Remarks":             ["technical remarks", "tech remarks", "tech notes", "developer comments"],
    "Color ":                        ["color", "flag", "highlight flag", "priority flag", "marker"]
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