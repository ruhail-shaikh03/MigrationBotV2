import logging
from typing import Dict, Any, List, Optional
from uuid import UUID

logger = logging.getLogger("session_memory")

class SessionMemory:
    """
    Maintains session-level context including recent tool execution results,
    frequently accessed RICEFW IDs, and active search filters.
    """
    def __init__(self):
        self._tool_cache: List[Dict[str, Any]] = []
        self._accessed_ids: List[str] = []
        self._last_search_filter: Optional[Dict[str, Any]] = None

    def record_tool_result(self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Cache tool execution result and extract useful entity references."""
        self._tool_cache.append({
            "tool_name": tool_name,
            "args": args,
            "result": result
        })
        if len(self._tool_cache) > 5:
            self._tool_cache.pop(0)

        # Extract RICEFW IDs if available
        if "ricefw_id" in args and args["ricefw_id"]:
            rid = str(args["ricefw_id"]).upper().strip()
            if rid not in self._accessed_ids:
                self._accessed_ids.append(rid)

    def get_recent_context_summary(self) -> str:
        """Generates a concise string summary of recent session context for LLM prompt context."""
        summary = []
        if self._accessed_ids:
            summary.append(f"Recently accessed RICEFW IDs: {', '.join(self._accessed_ids[-5:])}")
        if self._tool_cache:
            last = self._tool_cache[-1]
            summary.append(f"Last tool executed: {last['tool_name']}")
        return " | ".join(summary)
