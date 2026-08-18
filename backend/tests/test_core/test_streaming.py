"""Reassembling a streamed reply.

Streaming does not hand the loop a message object; it hands it fragments. Content arrives a
few characters at a time and a tool call arrives as a name, then an id, then its JSON
arguments split across however many deltas the provider chose. Getting that wrong does not
produce a visibly broken reply — it produces a tool call with truncated arguments, which
fails at dispatch, which is why every write would break and why this ships behind a flag
that is off by default.

The DSML tests are the ones that matter most. The loop retries on a different model when
the reasoner leaks its internal marker, and that check reads the *finished* reply. Streamed
naively, the marker would be on screen before the guard ever ran — so enabling streaming
would have silently disabled a safety feature that currently works.
"""

import pytest

from app.core.streaming import DSML_MARKER, DSMLLeak, complete


class Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class Call:
    def __init__(self, index=0, id=None, type=None, function=None):
        self.index = index
        self.id = id
        self.type = type
        self.function = function


class Delta:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class Chunk:
    def __init__(self, delta=None, choices=None):
        self.choices = choices if choices is not None else [type("C", (), {"delta": delta})()]


class FakeStreamingClient:
    """Stands in for AsyncOpenAI, yielding a scripted sequence of chunks."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **kwargs):
        chunks = self._chunks

        class Stream:
            def __aiter__(self):
                async def gen():
                    for chunk in chunks:
                        yield chunk

                return gen()

        return Stream()


class FakeBlockingClient:
    def __init__(self, message):
        self._message = message
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **kwargs):
        choice = type("Choice", (), {"message": self._message})()
        return type("Response", (), {"choices": [choice]})()


async def _collect(chunks):
    seen = []

    async def on_delta(text):
        seen.append(text)

    result = await complete(
        FakeStreamingClient(chunks), {"model": "x", "messages": []}, stream=True, on_delta=on_delta
    )
    return result, "".join(seen)


# --- the non-streaming path still normalises -----------------------------------

@pytest.mark.asyncio
async def test_a_blocking_reply_comes_back_in_wire_shape():
    message = type("M", (), {
        "content": "eleven items",
        "tool_calls": [Call(id="call_1", type="function", function=Fn("summarize", '{"a":1}'))],
        "reasoning_content": "thinking",
    })()
    result = await complete(FakeBlockingClient(message), {}, stream=False)

    assert result.content == "eleven items"
    assert result.tool_calls == [
        {"id": "call_1", "type": "function",
         "function": {"name": "summarize", "arguments": '{"a":1}'}}
    ]
    assert result.reasoning == "thinking"
    assert result.streamed is False


# --- content ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_content_deltas_are_concatenated_and_delivered():
    result, seen = await _collect([
        Chunk(Delta(content="Eleven ")),
        Chunk(Delta(content="items are ")),
        Chunk(Delta(content="overdue.")),
    ])
    assert result.content == "Eleven items are overdue."
    assert seen == "Eleven items are overdue."
    assert result.streamed is True


@pytest.mark.asyncio
async def test_a_chunk_carrying_no_choices_is_skipped_not_an_error():
    """Usage-only chunks arrive with an empty choices list."""
    result, _ = await _collect([
        Chunk(Delta(content="hi")),
        Chunk(choices=[]),
    ])
    assert result.content == "hi"


# --- tool calls ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_arguments_split_across_deltas_are_rejoined():
    """The failure this prevents is silent: truncated JSON fails at dispatch, not here."""
    result, _ = await _collect([
        Chunk(Delta(tool_calls=[Call(0, id="call_1", type="function", function=Fn("get_row", '{"ricefw'))])),
        Chunk(Delta(tool_calls=[Call(0, function=Fn(arguments='_id": "W-'))])),
        Chunk(Delta(tool_calls=[Call(0, function=Fn(arguments='1"}'))])),
    ])
    assert result.tool_calls == [
        {"id": "call_1", "type": "function",
         "function": {"name": "get_row", "arguments": '{"ricefw_id": "W-1"}'}}
    ]


@pytest.mark.asyncio
async def test_a_repeated_name_is_assigned_not_appended():
    """Concatenating would produce "get_rowget_row" — a tool nobody wrote."""
    result, _ = await _collect([
        Chunk(Delta(tool_calls=[Call(0, id="c1", function=Fn("get_row", "{}"))])),
        Chunk(Delta(tool_calls=[Call(0, function=Fn(name="get_row"))])),
    ])
    assert result.tool_calls[0]["function"]["name"] == "get_row"


@pytest.mark.asyncio
async def test_several_calls_are_kept_apart_by_index_and_returned_in_order():
    result, _ = await _collect([
        Chunk(Delta(tool_calls=[Call(1, id="c2", function=Fn("search_rows", "{}"))])),
        Chunk(Delta(tool_calls=[Call(0, id="c1", function=Fn("get_row", "{}"))])),
    ])
    assert [c["id"] for c in result.tool_calls] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_tool_call_fragments_are_never_shown_to_the_user():
    """A half-parsed tool call is not something to render; the loop announces tools itself."""
    _, seen = await _collect([
        Chunk(Delta(tool_calls=[Call(0, id="c1", function=Fn("get_row", '{"ricefw_id"'))])),
    ])
    assert seen == ""


@pytest.mark.asyncio
async def test_reasoning_deltas_are_collected_and_kept_off_the_wire():
    result, seen = await _collect([
        Chunk(Delta(reasoning_content="let me ")),
        Chunk(Delta(reasoning_content="think")),
        Chunk(Delta(content="answer")),
    ])
    assert result.reasoning == "let me think"
    assert seen == "answer"


# --- the leakage guard ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_marker_arriving_whole_stops_the_stream():
    with pytest.raises(DSMLLeak):
        await _collect([Chunk(Delta(content=f"hello {DSML_MARKER} world"))])


@pytest.mark.asyncio
async def test_a_marker_split_across_deltas_is_still_caught():
    """Checking each delta on its own would let this straight through."""
    head, tail = DSML_MARKER[:3], DSML_MARKER[3:]
    with pytest.raises(DSMLLeak):
        await _collect([Chunk(Delta(content="ok " + head)), Chunk(Delta(content=tail + " more"))])


@pytest.mark.asyncio
async def test_no_part_of_the_marker_reaches_the_client():
    head, tail = DSML_MARKER[:3], DSML_MARKER[3:]
    seen = []

    async def on_delta(text):
        seen.append(text)

    with pytest.raises(DSMLLeak) as exc:
        await complete(
            FakeStreamingClient([
                Chunk(Delta(content="The answer is " + head)),
                Chunk(Delta(content=tail + "leaked")),
            ]),
            {}, stream=True, on_delta=on_delta,
        )

    emitted = "".join(seen)
    assert DSML_MARKER not in emitted
    assert "leaked" not in emitted
    # And the caller is told what did get through, so it can retract it.
    assert exc.value.emitted == emitted


@pytest.mark.asyncio
async def test_a_clean_stream_releases_its_held_back_tail():
    """The last characters are withheld pending a possible marker; flush must free them."""
    result, seen = await _collect([Chunk(Delta(content="done."))])
    assert seen == "done."
    assert result.content == "done."


@pytest.mark.asyncio
async def test_text_that_merely_starts_like_the_marker_is_still_delivered():
    result, seen = await _collect([
        Chunk(Delta(content="a < b")),
        Chunk(Delta(content=" and c > d")),
    ])
    assert seen == result.content == "a < b and c > d"
