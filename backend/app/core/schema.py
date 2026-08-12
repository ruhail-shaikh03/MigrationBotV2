from typing import Any, Dict, List

from app.core.people import slugify_role

# schema_config (models/project.py:Project.schema_config) is JSONB in one of two
# shapes distinguished by a top-level "tabs" key: multi-tab (what
# schema_detect.py:detect_all_tabs produces) or flat, single-tab. Before this module
# existed the disambiguation was copy-pasted at seven call sites (TDD §16.20) —
# read.py, write.py (x3), worker.py, chat.py, agentic_loop.py — each spelled
# slightly differently. This is the one implementation.


def get_tab_schema(schema_config: Dict[str, Any], active_tab: str) -> Dict[str, Any]:
    """Resolve the effective per-tab schema dict for active_tab."""
    if "tabs" in schema_config:
        return schema_config.get("tabs", {}).get(active_tab, {})
    return schema_config


def get_people_columns(tab_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tab's people-columns, normalised to one shape for every caller.

    Detection now emits a `people_columns` list, because a sheet can name several
    responsible parties per row (a technical *and* a functional consultant, a QA owner,
    a business owner) and the old single `assignee_column` silently kept only one.

    Projects registered before that change still carry only `assignee_column`, and
    nothing re-detects them automatically (an admin re-runs detection from
    /admin/projects). So a legacy config is synthesised into a one-entry list here
    rather than at each call site: consumers read one shape, and an un-migrated project
    keeps working with the single role it always had instead of rendering no people at all.
    """
    columns = tab_schema.get("people_columns")
    if isinstance(columns, list) and columns:
        out = []
        for col in columns:
            header = col.get("header") if isinstance(col, dict) else None
            if not header:
                continue
            label = col.get("label") or str(header).strip()
            out.append({
                "key": col.get("key") or slugify_role(label),
                "label": label,
                "header": header,
            })
        if out:
            return out

    legacy = tab_schema.get("assignee_column")
    if legacy:
        label = str(legacy).strip()
        return [{"key": slugify_role(label), "label": label, "header": legacy}]

    return []


def get_effort_columns(tab_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tab's effort/duration columns, or [] when the sheet has none.

    An empty list is a normal, expected outcome — plenty of trackers record no
    level-of-effort at all. Callers must fall back to item counts and label them as
    counts rather than presenting a derived number as if the sheet had stated it.
    """
    columns = tab_schema.get("effort_columns")
    if not isinstance(columns, list):
        return []
    out = []
    for col in columns:
        header = col.get("header") if isinstance(col, dict) else None
        if not header:
            continue
        label = col.get("label") or str(header).strip()
        out.append({
            "key": col.get("key") or slugify_role(label),
            "label": label,
            "header": header,
            "unit": col.get("unit") or "days",
        })
    return out


def get_available_tabs(schema_config: Dict[str, Any]) -> List[str]:
    """Resolve the tab names this project can switch between, for system-prompt injection.

    Replaces the old get_valid_modules(). The rename is the point: that function returned
    tab names but was injected into the prompt as "Valid modules: …", which the model then
    enforced as an allowlist of ID prefixes. On a single-tab sheet it returned nothing, and
    the prompt substituted a hardcoded SAP list, so an ID like SLCM-0586 was refused on a
    sheet that had never heard of SAP modules. Tabs are a navigation concept (switch_module);
    they are not a vocabulary of legal identifiers, and nothing derives one from the other.

    A flat, single-tab config has nowhere to switch to, so it yields an empty list.
    """
    if "tabs" in schema_config:
        return list(schema_config.get("tabs", {}).keys())
    return []
