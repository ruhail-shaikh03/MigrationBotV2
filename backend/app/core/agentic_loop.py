import json
import logging
from typing import List, Dict, Any, Callable, Awaitable, Optional
from openai import AsyncOpenAI
from app.core.llm_router import select_model
from app.core.tool_schemas import TOOLS, get_system_prompt, get_system_prompt_compact
from app.core.permissions import PermissionChecker
from app.core.tool_dispatch import dispatch_tool
from app.core.errors import failure_note, PERMISSION
from app.core.schema import get_available_tabs
from app.core.streaming import DSML_MARKER, DSMLLeak, complete
from app.config import settings
from app.core.column_mapper import get_column_map_json

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
    google_refresh_token: Optional[str] = None,
    max_iterations: int = 8,
) -> List[Dict[str, Any]]:
    """
    Executes the multi-turn agentic loop. Receives user queries, interacts with DeepSeek,
    enforces RBAC permissions, routes tool requests to the dispatcher, and returns the
    updated message history.

    Replies are streamed only when `settings.STREAM_RESPONSES` is on; the default is a
    single `assistant` frame carrying the whole reply. This docstring claimed token
    streaming for months while the code contained no `stream=True` anywhere — the claim is
    now conditional and true either way.
    """
    available_tabs = get_available_tabs(schema_config)
    column_map_json = get_column_map_json(column_map)

    # Read once per turn rather than per iteration, so a config reload mid-turn cannot
    # switch delivery modes halfway through one reply.
    streaming = bool(getattr(settings, "STREAM_RESPONSES", False))

    # Generate initial full system prompt
    system_prompt = get_system_prompt(available_tabs, column_map_json)
    
    # Reconstruct messages context for the LLM
    messages = [{"role": "system", "content": system_prompt}] + message_history + [{"role": "user", "content": user_message}]
    
    for iteration in range(max_iterations):
        # Swap compact system prompt for iterations > 0 to save tokens
        if iteration > 0:
            messages[0] = {"role": "system", "content": get_system_prompt_compact(available_tabs)}
            
        model = select_model(iteration, messages)
        logger.info(f"Iteration {iteration}: Routing to model {model}")
        
        try:
            # Prepare API arguments
            api_kwargs = {
                "model": model,
                "messages": messages,
                "tools": TOOLS
            }

            # On the last permitted step, withdraw the tools so the model has to speak.
            # Previously a loop that spent every iteration on tool calls ended with the
            # `else` branch below — an error frame and nothing else — throwing away
            # everything it had already read from the sheet. Observed in production on
            # "what's overdue?": nine tool results gathered, zero delivered.
            if iteration == max_iterations - 1:
                api_kwargs["messages"] = messages + [{
                    "role": "system",
                    "content": (
                        "This is your final step; no further tool calls are possible. "
                        "Answer now using only what the tool results above already "
                        "contain. If they are incomplete, state plainly what you did "
                        "establish and what remains unknown, and suggest a narrower "
                        "question. Do not apologise for running out of steps."
                    ),
                }]
                api_kwargs.pop("tools")

            async def emit_delta(fragment: str) -> None:
                await send_websocket_msg({
                    "type": "assistant",
                    "content": fragment,
                    "done": False,
                })

            try:
                message = await complete(
                    llm_client, api_kwargs, stream=streaming, on_delta=emit_delta
                )
            except DSMLLeak as leak:
                # Caught here rather than inside the stream because retracting is the
                # caller's business: the client has already rendered `leak.emitted`, and a
                # retry appends to it. The reset frame tells it to clear that bubble first,
                # or the user reads the good answer stapled to the tail of a poisoned one.
                logger.warning("DSML Leakage detected mid-stream! Retrying with deepseek-chat.")
                if leak.emitted:
                    await send_websocket_msg({"type": "assistant", "content": "", "reset": True})
                message = await complete(
                    llm_client, {**api_kwargs, "model": "deepseek-chat"},
                    stream=streaming, on_delta=emit_delta,
                )

            content = message.content

            # DSML Leakage Guard, non-streaming path. Streaming is guarded inside
            # `complete`, which has to stop the marker before it is emitted rather than
            # after the reply is whole.
            if DSML_MARKER in content:
                logger.warning("DSML Leakage detected! Aborting and retrying with deepseek-chat.")
                # Force fallback to V3 Chat model for safety
                # Same kwargs as the call that leaked, model forced to V3 — in particular
                # the final-step variant must retry *without* tools too, or the retry
                # reopens the tool path the outer call just closed.
                message = await complete(llm_client, {**api_kwargs, "model": "deepseek-chat"})
                content = message.content
            
            # Prevent Chain of Thought (CoT) leaking to client
            # Extract reasoning_content (logged internally only)
            if message.reasoning:
                logger.info(f"Reasoner CoT reasoning: {message.reasoning}")
            
            # Format and save assistant message to history
            assistant_msg = {"role": "assistant", "content": content}
            if message.tool_calls:
                assistant_msg["tool_calls"] = message.tool_calls
            messages.append(assistant_msg)
            
            # If no tools called, we have completed the request
            if not message.tool_calls:
                await send_websocket_msg({
                    "type": "assistant",
                    # Already delivered fragment by fragment when streaming; repeating the
                    # whole thing here would render it twice, since the client appends.
                    "content": "" if message.streamed else content,
                    "done": True
                })
                break
                
            # Process all tool calls in sequence
            for tool_call in message.tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_args_str = tool_call["function"]["arguments"]
                
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
                    tool_result = {
                        "ok": False,
                        "error": f"Permission denied: {reason}",
                        "error_kind": PERMISSION,
                        "user_message": f"You don't have permission to do that: {reason}",
                    }
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
                        google_access_token=google_access_token,
                        google_refresh_token=google_refresh_token
                    )
                
                # Send result back to client and append to context
                await send_websocket_msg({
                    "type": "tool_result",
                    "tool": tool_name,
                    "result": tool_result
                })

                # Format tool response content; on failure, append guidance chosen for
                # that failure's class. Previously every failure — outage, permission
                # denial, bad column alike — got the same "formulate a corrected tool
                # call" note, so the model retried things that could never succeed and
                # described infrastructure faults to the user as rejected edits.
                content_str = json.dumps(tool_result, ensure_ascii=False)
                if not tool_result.get("ok"):
                    content_str += failure_note(tool_name, tool_result)
                elif tool_result.get("status") == "queued":
                    # Reinforced here, not just in the system prompt: from iteration 1 onward
                    # the system message is swapped for the compact variant (see below), which
                    # doesn't carry the RULES section — this note is the only guaranteed place
                    # the model still sees the "don't claim this is done" instruction.
                    content_str += (
                        f"\n[System Note]: '{tool_name}' has been queued, not yet applied to the "
                        "spreadsheet. Do not tell the user it is done — phrase your reply as pending."
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_name,
                    "content": content_str
                })
                
        except Exception as e:
            logger.error(f"Exception in agentic reasoning loop: {e}")
            await send_websocket_msg({
                "type": "error",
                "message": f"System error during agent reasoning: {str(e)}"
            })
            break
    else:
        # Loop exhausted max_iterations without a `break` (no final text answer, no
        # exception) — the model kept calling tools. Without this, the client never
        # gets an "assistant" or "error" frame and the UI spins forever.
        logger.warning(f"Agentic loop exhausted {max_iterations} iterations without a final answer.")
        await send_websocket_msg({
            "type": "error",
            "message": f"I couldn't finish this request within {max_iterations} steps. Try rephrasing it or breaking it into smaller requests."
        })

    # Return updated message history (slice off system prompt)
    return messages[1:]
