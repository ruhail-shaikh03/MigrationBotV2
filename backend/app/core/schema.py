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
