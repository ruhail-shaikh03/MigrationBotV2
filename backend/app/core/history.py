"""Moving a conversation between the three shapes it has to exist in.

There are three, and conflating any two of them breaks something:

* **Wire shape** — what the LLM sends and receives. `{"role": "assistant", "content": None,
  "tool_calls": [...]}` followed by `{"role": "tool", "tool_call_id": ..., "content": ...}`.
  This is what `agentic_loop.run_agentic_loop` returns and consumes, and it is what gets
  stored, because it is the only shape that can be replayed to the model faithfully.
* **Storage shape** — columns on `models/message.py:Message`.
* **Display shape** — what the chat UI renders: one bubble per turn, with the tool calls
  folded into the assistant bubble that made them.

The wire and display shapes differ by more than field names. A single assistant *bubble*
on screen is several wire messages — one assistant message per round of tool calls, a tool
message per result, then a final assistant message carrying the prose. Replaying the wire
messages one-to-one would render each round as its own empty bubble, which is not what the
user saw the first time.
"""

import json
from typing import Any, Dict, Iterable, List, Optional

# How much conversation the *user* gets back on reconnect. Generous: it costs one indexed
# query and some JSON, and a chat that reappears half-missing reads as data loss.
MAX_REPLAY_MESSAGES = 200

# How much the *model* gets as context. Deliberately much smaller and deliberately not the
# same number. The loop already swaps in a compact system prompt from iteration 1 to hold
# token use down (§8.1); feeding it an unbounded history would undo that saving on the
# very next turn, and every turn after it, at a cost that grows with the conversation.
MAX_MODEL_MESSAGES = 24


def to_wire(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    """Storage rows -> the shape `run_agentic_loop` expects as `message_history`.

    Keys absent rather than null where the API treats them differently: an assistant
    message carrying `"tool_calls": None` is not the same as one with no `tool_calls` key.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        message: Dict[str, Any] = {"role": row.role}
        # Present-but-None is correct for an assistant message that only called tools;
        # the key has to exist for the API to accept it.
        if row.role == "assistant":
            message["content"] = row.content
        else:
            message["content"] = row.content or ""
        if row.tool_calls:
            message["tool_calls"] = row.tool_calls
        if row.tool_call_id:
            message["tool_call_id"] = row.tool_call_id
        out.append(message)
    return out


def to_storage(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Wire messages -> kwargs for `Message(...)`, skipping anything not worth keeping.

    System messages are dropped. They are rebuilt every turn from the live project, tab
    and schema, so a stored copy would come back describing a sheet that may since have
    been re-detected — stale in exactly the way that is hardest to notice.
    """
    out: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        tool_calls = message.get("tool_calls")
        out.append({
            "role": role,
            "content": message.get("content"),
            # Normalised through JSON so a provider SDK object is stored as plain data.
            # Passing the SDK's own object straight to JSONB fails to serialise, and it
            # fails at commit time — after the answer has already been sent to the user.
            "tool_calls": _jsonable(tool_calls) if tool_calls else None,
            "tool_call_id": message.get("tool_call_id"),
        })
    return out


def trim_for_model(
    messages: List[Dict[str, Any]], max_messages: int = MAX_MODEL_MESSAGES
) -> List[Dict[str, Any]]:
    """The most recent slice of a conversation that is still valid to send.

    Trimming a chat history is not slicing. A `tool` message is only meaningful as a reply
    to the `assistant` message whose `tool_calls` it answers, and the API rejects the
    request outright when that parent is missing — so a naive `messages[-24:]` fails
    whenever the cut happens to land mid-turn, which is most of the time on a
    tool-heavy conversation and never in a short manual test.

    So the window is opened at the nearest `user` message, which starts a whole turn. If
    the window contains none — one very long tool-calling turn — it is opened after any
    leading orphan `tool` messages instead, which is the weaker guarantee but still valid.
    """
    if max_messages <= 0 or len(messages) <= max_messages:
        return _drop_leading_orphans(messages)

    window = messages[-max_messages:]
    for i, message in enumerate(window):
        if message.get("role") == "user":
            return window[i:]
    return _drop_leading_orphans(window)


def _drop_leading_orphans(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    i = 0
    while i < len(messages) and messages[i].get("role") == "tool":
        i += 1
    return messages[i:]


def to_display(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Wire messages -> the bubbles the chat UI renders, in the shape it already uses.

    A run of assistant and tool messages collapses into one bubble, because that is what
    the live socket produces: `tool_start` appends a tool call to the current assistant
    bubble and the final `assistant` frame appends prose to that same bubble. Emitting one
    bubble per wire message would replay a single answer as three or four, most of them
    blank.

    Every tool call comes back as `completed`. The status is a live-progress indicator —
    a call still `running` when the socket dropped has no outcome to report, and showing a
    spinner for it forever would be a worse lie than showing it as finished.
    """
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    # Tool results arrive after the call that produced them, so calls are indexed by id
    # and filled in as the replies are walked past.
    by_call_id: Dict[str, Dict[str, Any]] = {}

    for message in messages:
        role = message.get("role")

        if role == "user":
            current = None
            by_call_id = {}
            out.append({
                "role": "user",
                "content": message.get("content") or "",
                "toolCalls": [],
            })
            continue

        if role == "assistant":
            if current is None:
                current = {"role": "assistant", "content": "", "toolCalls": []}
                out.append(current)
            if message.get("content"):
                current["content"] += message["content"]
            for call in message.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                entry = {
                    "name": function.get("name") or "",
                    "args": _loads(function.get("arguments")),
                    "status": "completed",
                    "result": None,
                }
                current["toolCalls"].append(entry)
                if call.get("id"):
                    by_call_id[call["id"]] = entry
            continue

        if role == "tool":
            entry = by_call_id.get(message.get("tool_call_id") or "")
            if entry is not None:
                entry["result"] = _loads(message.get("content"))
                # The tool itself reports failure in its payload; `ok: False` is how every
                # dispatch signals it (§8.2), so the pill matches what the run actually did
                # rather than reporting every replayed call as a success.
                if isinstance(entry["result"], dict) and entry["result"].get("ok") is False:
                    entry["status"] = "failed"

    return [m for m in out if m["content"] or m["toolCalls"]]


def _loads(raw: Any) -> Any:
    """Parse a JSON payload, or hand back the raw text when it is not JSON."""
    if raw is None or isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _jsonable(value: Any) -> Any:
    """Coerce provider SDK objects into plain data JSONB can hold."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    for attr in ("model_dump", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return _jsonable(method())
            except Exception:
                break
    return str(value)
