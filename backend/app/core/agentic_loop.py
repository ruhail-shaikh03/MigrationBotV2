import json
import logging
from typing import List, Dict, Any, Callable, Awaitable
from openai import AsyncOpenAI
from app.core.llm_router import select_model
from app.core.tool_schemas import TOOLS, get_system_prompt, get_system_prompt_compact
from app.core.permissions import PermissionChecker
from app.core.tool_dispatch import dispatch_tool

logger = logging.getLogger("agentic_loop")

async def run_agentic_loop(
    user_message: str,
    message_history: List[Dict[str, Any]],
    user_email: str,
    session_id: Any,
    spreadsheet_id: str,
    active_tab: str,
    schema_config: dict,
    column_map: dict,
    checker: PermissionChecker,
    llm_client: AsyncOpenAI,
    send_websocket_msg: Callable[[Dict[str, Any]], Awaitable[None]],
    db_session: Any,
    google_access_token: str = "mock-google-access-token",
    max_iterations: int = 8,
) -> List[Dict[str, Any]]:
    """
    Executes the multi-turn agentic loop. Receives user queries, interacts with DeepSeek,
    enforces RBAC permissions, routes tool requests to the dispatcher, and streams replies.
    """
    valid_modules = schema_config.get("valid_modules", [])
    column_map_json = json.dumps(column_map, ensure_ascii=False)
    
    # Generate initial full system prompt
    system_prompt = get_system_prompt(valid_modules, column_map_json)
    
    # Reconstruct messages context for the LLM
    messages = [{"role": "system", "content": system_prompt}] + message_history + [{"role": "user", "content": user_message}]
    
    for iteration in range(max_iterations):
        # Swap compact system prompt for iterations > 0 to save tokens
        if iteration > 0:
            messages[0] = {"role": "system", "content": get_system_prompt_compact(valid_modules)}
            
        model = select_model(iteration, messages)
        logger.info(f"Iteration {iteration}: Routing to model {model}")
        
        try:
            # Prepare API arguments
            api_kwargs = {
                "model": model,
                "messages": messages,
                "tools": TOOLS
            }
            
            response = await llm_client.chat.completions.create(**api_kwargs)
            choice = response.choices[0]
            message = choice.message
            content = message.content or ""
            
            # DSML Leakage Guard
            if "<｜｜DSML｜｜>" in content:
                logger.warning("DSML Leakage detected! Aborting and retrying with deepseek-chat.")
                # Force fallback to V3 Chat model for safety
                retry_response = await llm_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    tools=TOOLS
                )
                choice = retry_response.choices[0]
                message = choice.message
                content = message.content or ""
            
            # Prevent Chain of Thought (CoT) leaking to client
            # Extract reasoning_content (logged internally only)
            reasoning_content = getattr(message, "reasoning_content", None)
            if reasoning_content:
                logger.info(f"Reasoner CoT reasoning: {reasoning_content}")
            
            # Format and save assistant message to history
            assistant_msg = {"role": "assistant", "content": content}
            if message.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    } for tc in message.tool_calls
                ]
            messages.append(assistant_msg)
            
            # If no tools called, we have completed the request
            if not message.tool_calls:
                await send_websocket_msg({
                    "type": "assistant",
                    "content": content,
                    "done": True
                })
                break
                
            # Process all tool calls in sequence
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args_str = tool_call.function.arguments
                
                try:
                    tool_args = json.loads(tool_args_str)
                except Exception as e:
                    tool_args = {}
                    logger.error(f"Failed to parse tool arguments JSON: {e}")
                
                # Send tool activation status message
                await send_websocket_msg({
                    "type": "tool_start",
                    "tool": tool_name,
                    "args": tool_args
                })
                
                # Check permissions (RBAC interception)
                allowed, reason = checker.can_execute(tool_name, tool_args)
                if not allowed:
                    logger.warning(f"RBAC Blocked tool {tool_name} for user {checker.email}: {reason}")
                    tool_result = {"ok": False, "error": f"Permission denied: {reason}"}
                    await send_websocket_msg({
                        "type": "error",
                        "message": f"Permission denied: {reason}"
                    })
                else:
                    # Execute tool via dispatcher
                    tool_result = await dispatch_tool(
                        tool_name=tool_name,
                        args=tool_args,
                        user_email=user_email,
                        session_id=session_id,
                        spreadsheet_id=spreadsheet_id,
                        active_tab=active_tab,
                        schema_config=schema_config,
                        column_map=column_map,
                        db_session=db_session,
                        google_access_token=google_access_token
                    )
                
                # Send result back to client and append to context
                await send_websocket_msg({
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": tool_result
                })
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": json.dumps(tool_result, ensure_ascii=False)
                })
                
        except Exception as e:
            logger.error(f"Exception in agentic reasoning loop: {e}")
            await send_websocket_msg({
                "type": "error",
                "message": f"System error during agent reasoning: {str(e)}"
            })
            break
            
    # Return updated message history (slice off system prompt)
    return messages[1:]
