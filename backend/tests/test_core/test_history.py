"""Conversation replay: wire shape in, storage and display shapes out.

The bug behind all of this is that `chat.py` held the conversation in a local
`message_history = []`, rebuilt empty on every connection. What made it hard to notice is
that the browser store is a module-scope singleton, so after the 3-second reconnect the
*user* still saw the whole conversation while the *model* had forgotten it — the screen
and the answers disagreed, and only the answers were wrong.

Two things here are easy to get wrong in ways no short manual test would reveal:

* Trimming a chat history is not slicing. A `tool` message orphaned from the `assistant`
  message whose `tool_calls` it answers is rejected by the API outright, and a naive
  `messages[-N:]` produces one whenever the cut lands mid-turn.
* One assistant *bubble* is several wire messages. Replaying them one-to-one renders a
  single answer as three or four bubbles, most of them blank.
"""

from app.core.history import (
    MAX_MODEL_MESSAGES, to_display, to_storage, to_wire, trim_for_model,
)


def _assistant_calling(name, call_id, args='{"ricefw_id": "W-1"}'):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
        ],
    }


def _tool_reply(call_id, payload='{"ok": true, "rows": 3}'):
    return {"role": "tool", "tool_call_id": call_id, "content": payload}


# --- trimming ----------------------------------------------------------------

def test_a_short_history_is_returned_whole():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert trim_for_model(messages) == messages


def test_trimming_never_leaves_a_tool_message_without_its_call():
    """The failure mode: the API rejects an orphaned tool message outright."""
    messages = [
        {"role": "user", "content": "first"},
        _assistant_calling("search_rows", "call_1"),
        _tool_reply("call_1"),
        {"role": "assistant", "content": "three rows"},
    ]
    trimmed = trim_for_model(messages, max_messages=2)
    assert trimmed[0]["role"] != "tool"


def test_the_window_opens_at_a_whole_turn_when_one_is_available():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "two"},
    ]
    trimmed = trim_for_model(messages, max_messages=3)
    assert trimmed == messages[2:]


def test_a_window_containing_no_user_message_still_returns_something_valid():
    """One very long tool-calling turn. The weaker guarantee, but never an orphan."""
    messages = [{"role": "user", "content": "go"}]
    for i in range(6):
        messages.append(_assistant_calling("summarize", f"call_{i}"))
        messages.append(_tool_reply(f"call_{i}"))
    trimmed = trim_for_model(messages, max_messages=4)
    assert trimmed
    assert trimmed[0]["role"] != "tool"


def test_the_model_window_is_smaller_than_the_replay_window():
    """Two different limits on purpose: the compact system prompt exists to save tokens."""
    from app.core.history import MAX_REPLAY_MESSAGES
    assert MAX_MODEL_MESSAGES < MAX_REPLAY_MESSAGES


# --- storage -----------------------------------------------------------------

def test_the_system_prompt_is_never_stored():
    """It is rebuilt each turn from the live project; a stored copy comes back stale."""
    rows = to_storage([
        {"role": "system", "content": "You are..."},
        {"role": "user", "content": "hi"},
    ])
    assert [r["role"] for r in rows] == ["user"]


def test_an_assistant_that_only_called_tools_keeps_its_null_content():
    """Coercing it to "" would replay as an empty assistant turn."""
    rows = to_storage([_assistant_calling("get_row", "call_1")])
    assert rows[0]["content"] is None
    assert rows[0]["tool_calls"][0]["id"] == "call_1"


def test_tool_call_ids_survive_storage():
    """Without the id the reply cannot be paired back to its call, and replay is invalid."""
    rows = to_storage([_tool_reply("call_9")])
    assert rows[0]["tool_call_id"] == "call_9"


def test_sdk_objects_are_normalised_to_plain_data():
    """A provider object handed straight to JSONB fails at commit — after the user has
    already been sent the answer."""
    class FakeSDKCall:
        def model_dump(self):
            return {"id": "call_1", "function": {"name": "get_row", "arguments": "{}"}}

    rows = to_storage([{"role": "assistant", "content": None, "tool_calls": [FakeSDKCall()]}])
    assert rows[0]["tool_calls"] == [
        {"id": "call_1", "function": {"name": "get_row", "arguments": "{}"}}
    ]


def test_storage_and_wire_shapes_round_trip():
    class Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    original = [
        {"role": "user", "content": "what is overdue"},
        _assistant_calling("summarize", "call_1"),
        _tool_reply("call_1"),
        {"role": "assistant", "content": "Eleven items."},
    ]
    rows = [Row(**r) for r in to_storage(original)]
    assert to_wire(rows) == original


# --- display -----------------------------------------------------------------

def test_one_answer_renders_as_one_bubble_not_three():
    """The live socket appends tool calls and prose to the same assistant bubble."""
    display = to_display([
        {"role": "user", "content": "what is overdue"},
        _assistant_calling("summarize", "call_1"),
        _tool_reply("call_1"),
        {"role": "assistant", "content": "Eleven items."},
    ])
    assert [m["role"] for m in display] == ["user", "assistant"]
    assert display[1]["content"] == "Eleven items."
    assert len(display[1]["toolCalls"]) == 1


def test_a_tool_result_is_paired_back_to_the_call_that_produced_it():
    display = to_display([
        {"role": "user", "content": "go"},
        _assistant_calling("search_rows", "call_1"),
        _tool_reply("call_1", '{"ok": true, "rows": 3}'),
        {"role": "assistant", "content": "done"},
    ])
    call = display[1]["toolCalls"][0]
    assert call["name"] == "search_rows"
    assert call["args"] == {"ricefw_id": "W-1"}
    assert call["result"] == {"ok": True, "rows": 3}
    assert call["status"] == "completed"


def test_a_failed_tool_call_replays_as_failed():
    """`ok: False` is how every dispatch reports failure; replaying it green would lie."""
    display = to_display([
        {"role": "user", "content": "go"},
        _assistant_calling("update_cell", "call_1"),
        _tool_reply("call_1", '{"ok": false, "error": "not found"}'),
    ])
    assert display[1]["toolCalls"][0]["status"] == "failed"


def test_a_tool_payload_that_is_not_json_survives_as_text():
    display = to_display([
        {"role": "user", "content": "go"},
        _assistant_calling("get_row", "call_1"),
        _tool_reply("call_1", "plain text failure"),
    ])
    assert display[1]["toolCalls"][0]["result"] == "plain text failure"


def test_empty_bubbles_are_dropped():
    """An assistant message with no prose and no calls has nothing to render."""
    display = to_display([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
    ])
    assert [m["role"] for m in display] == ["user"]


def test_a_second_user_message_starts_a_new_bubble():
    display = to_display([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "two"},
    ])
    assert [m["content"] for m in display] == ["first", "one", "second", "two"]
