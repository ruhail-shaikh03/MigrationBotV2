import json
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("planner")

class PlanStep(BaseModel):
    step_id: int
    tool_name: str
    description: str
    args: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[int] = Field(default_factory=list)
    can_parallel: bool = False
    status: str = "pending"  # pending, running, completed, failed
    result: Optional[Dict[str, Any]] = None

class ExecutionPlan(BaseModel):
    user_request: str
    steps: List[PlanStep] = Field(default_factory=list)

class AgenticPlanner:
    """
    Decomposes complex, multi-step migration tracker requests into structured,
    executable step sequences.
    """
    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    async def create_plan(
        self,
        user_message: str,
        project_name: str,
        active_tab: str,
        available_tools: List[str]
    ) -> Optional[ExecutionPlan]:
        """Generate a structured execution plan from the user prompt."""
        planner_prompt = f"""
You are a task planner for an S/4HANA migration tracker AI agent.

User Request: "{user_message}"
Context: Project="{project_name}", Active Tab="{active_tab}"
Available Tools: {", ".join(available_tools)}

If the request requires multiple steps (e.g., search first, then update), decompose it into structured steps.
Return ONLY valid JSON with format:
{{
  "user_request": "{user_message}",
  "steps": [
    {{
      "step_id": 1,
      "tool_name": "<tool_name>",
      "description": "<brief summary of step>",
      "args": {{}},
      "depends_on": [],
      "can_parallel": false
    }}
  ]
}}
"""
        try:
            response = await self.llm_client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": planner_prompt}],
                temperature=0.1,
                max_tokens=1024
            )
            content = response.choices[0].message.content.strip()
            cleaned = content.strip("`").replace("json\n", "")
            data = json.loads(cleaned)
            return ExecutionPlan(**data)
        except Exception as e:
            logger.warning(f"Plan generation failed: {e}")
            return None
