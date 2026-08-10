---
name: migrationbot-agentic-loop-eval
description: Evaluate, test, or debug MigrationBot's agentic LLM loop in backend/app/core/agentic_loop.py — iteration caps, DeepSeek model routing via llm_router.py, DSML leakage guard, system prompt swapping, tool_call dispatch, and WebSocket message streaming. Use when mocking AsyncOpenAI multi-turn responses, adding a tool to tool_schemas.py, diagnosing runaway or truncated loops, or checking RBAC enforcement inside the loop.
---

# Evaluating the MigrationBot Agentic Loop

## Loop mechanics you must preserve

`core/agentic_loop.py:run_agentic_loop()`:

- **`max_iterations: int = 8`**, a hard cap. Exceeding it terminates the loop, not the request.
  A task needing >8 round-trips is a prompt/tool-design problem — raising the cap is almost
  never the fix.
- **System prompt swaps by iteration.** Iteration 0 sends the full `SYSTEM_PROMPT` (with
  `column_map` JSON and schema context); iterations >0 send `SYSTEM_PROMPT_COMPACT` to save
  tokens. Both live in `core/tool_schemas.py`.
- **DSML leakage guard.** If response content contains the `DSML` marker sentinel, the
  response is discarded and retried against `deepseek-chat` (never `deepseek-reasoner`).
- **CoT is never forwarded.** `reasoning_content` from `deepseek-reasoner` is logged
  internally and must not reach the client.

## Model routing

`core/llm_router.py:select_model()` picks `deepseek-reasoner` only at iteration 0 when
`has_conditional_logic()` matches conditional keywords (`if`, `only if`, `check first`,
`depending on`, `unless`, `conditional`, `where`); otherwise `deepseek-chat`. Tests asserting
a model choice must control both the iteration number and the prompt text.

## Mocking multi-turn DeepSeek responses

Use `AsyncMock` with `side_effect` as a script — one entry per expected round trip. The mock
must mirror the OpenAI response shape the loop destructures:

```python
from unittest.mock import AsyncMock, MagicMock

def tool_turn(name, args_json):
    tc = MagicMock()
    tc.id = "call_1"
    tc.type = "function"
    tc.function.name = name
    tc.function.arguments = args_json      # JSON *string*, not a dict
    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc]
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg, finish_reason="tool_calls")]
    return resp

def text_turn(text):
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = None
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg, finish_reason="stop")]
    return resp

client = AsyncMock()
client.chat.completions.create.side_effect = [
    tool_turn("get_row", '{"ricefw_id": "SD-045"}'),
    text_turn("SD-045 is In Progress."),
]
```

`function.arguments` is a JSON **string** — the loop `json.loads()` it. Passing a dict is the
most common reason a mocked loop test fails with a confusing parse error.

## Asserting on the WebSocket stream

The loop reports progress through a `send_msg` callback. Capture it and assert on the emitted
sequence, which is the real contract with the frontend:

- `{"type": "tool_start", "tool": ..., "args": {...}}` — before each dispatch
- `{"type": "tool_result", "tool": ..., "result": {...}}` — after each dispatch
- `{"type": "assistant", "content": ..., "done": true}` — final text, sent whole (not token-streamed)

`tests/test_core/test_core_logic.py::test_agentic_loop_max_iterations` asserts exactly 8
`tool_start` messages when the mocked LLM never stops calling tools — the canonical
iteration-cap test.

## Adding a tool — four files move together

1. `core/tool_schemas.py` → append to `TOOLS` (OpenAI function-calling dict).
2. `core/tool_dispatch.py` → add the name to the read tuple (line ~26) or write tuple
   (line ~64) and wire the call. **These are inline tuples, not shared constants.**
3. `core/permissions.py` → add to `READ_ONLY_TOOLS` or `WRITE_TOOLS` (lines 8–9).
   Omitting this silently blocks viewers — the exact bug `data_quality` once had.
4. Write tools only: `queue/worker.py:process_job()` needs a branch, or the job dequeues
   and vanishes.

The tuples in `tool_dispatch.py` and the sets in `permissions.py` are **duplicated lists that
must stay in sync**. Drift between them is the highest-frequency defect in this subsystem.

## Known evaluation gaps

- **RBAC is not tested through the loop.** `PermissionChecker` is unit-tested in isolation
  (`test_rbac_interception`); no test proves `run_agentic_loop` actually calls it and aborts
  a denied tool. If you touch permission handling in the loop, you are working without a net.
- **`dispatch_tool` is patched out** in `integration/test_integration_logic.py::test_e2e_read_flow`,
  so routing logic is never exercised end-to-end.
- **No write-path loop test.** The write integration test bypasses the loop and calls
  `process_job` / `enqueue_write_job` directly.
- Untested: malformed tool arguments, multiple `tool_calls` in one turn, DSML guard triggering.
