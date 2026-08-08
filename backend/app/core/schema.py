from typing import Any, Dict, List

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


def get_valid_modules(schema_config: Dict[str, Any]) -> List[str]:
    """Resolve the list of valid module/tab names for system-prompt injection."""
    if "tabs" in schema_config:
        return schema_config.get("global", {}).get("valid_modules") or list(schema_config.get("tabs", {}).keys())
    return schema_config.get("valid_modules", [])
