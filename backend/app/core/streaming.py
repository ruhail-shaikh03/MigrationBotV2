"""One reply, assembled the same way whether the provider streamed it or not.

`agentic_loop.py` used to read the SDK's response object directly — `message.content`,
`message.tool_calls[i].function.arguments`. Streaming does not hand you that object. It
hands you fragments: content arrives a few characters at a time, and a tool call arrives as
a name, then an id, then its JSON arguments split across however many deltas the provider
felt like. Reassembling those is the entire difficulty of turning streaming on, and it is
why this lives in its own module with its own tests rather than inline in the loop.

`complete()` returns the same `CompletedMessage` either way, in plain wire shape, so the
loop has one code path and the streaming flag changes only how the frames arrive.

**The leakage guard is why this is not a trivial refactor.** The loop aborts and retries on
a different model when `<｜｜DSML｜｜>` appears in the reply — a real artifact seen in
production from the reasoner. That check reads the *finished* reply. Streamed naively, the
leaked marker would already be on the user's screen before the guard ever ran, and turning
the flag on would silently disable a safety feature that currently works. So the emitter
below withholds any trailing text that could still turn out to be the start of the marker,
and raises before a single character of it escapes.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("streaming")

# The reasoner's internal-markup marker. Matched verbatim; these are full-width bars, not
# ASCII pipes, and normalising them would break the match.
DSML_MARKER = "<｜｜DSML｜｜>"


class DSMLLeak(Exception):
    """Raised mid-stream the moment the marker completes, before it can be emitted.

    Carries the text already sent to the client, so the caller knows there is something on
    screen that needs retracting rather than merely appending to.
    """

    def __init__(self, emitted: str = "") -> None:
        super().__init__("DSML marker detected in streamed content")
        self.emitted = emitted


@dataclass
class CompletedMessage:
    """A finished assistant turn, in the shape the rest of the app already speaks."""

    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    reasoning: Optional[str] = None
    # True when the text was already delivered to the client delta by delta, so the caller
    # must not send it a second time as a whole.
    streamed: bool = False


class _SafeEmitter:
    """Emits streamed text, holding back anything that might become the DSML marker.

    A marker can straddle deltas — "…<｜" in one chunk and "｜DSML｜｜>" in the next — so
    checking each delta on its own would let it through. Instead the last `len(marker) - 1`
    characters are held until the following delta proves them harmless. The user sees a lag
    of a dozen characters at the very tail of the stream and nothing else.
    """

    def __init__(self, on_delta: Optional[Callable[[str], Awaitable[None]]]) -> None:
        self._on_delta = on_delta
        self._pending = ""
        self.emitted = ""
        self.full = ""

    async def feed(self, text: str) -> None:
        if not text:
            return
        self.full += text
        self._pending += text

        if DSML_MARKER in self._pending:
            # Everything before the marker is legitimate and has either been emitted or is
            # about to be discarded with the retry; nothing after it may ever be sent.
            raise DSMLLeak(self.emitted)

        keep = len(DSML_MARKER) - 1
        if len(self._pending) > keep:
            release, self._pending = self._pending[:-keep], self._pending[-keep:]
            await self._send(release)

    async def flush(self) -> None:
        """Release the held-back tail once the stream has ended cleanly."""
        if self._pending:
            release, self._pending = self._pending, ""
            await self._send(release)

    async def _send(self, text: str) -> None:
        self.emitted += text
        if self._on_delta:
            await self._on_delta(text)


def _call_to_dict(call: Any) -> Dict[str, Any]:
    """Normalise one SDK tool-call object into wire shape."""
    function = getattr(call, "function", None)
    return {
        "id": getattr(call, "id", "") or "",
        "type": getattr(call, "type", "function") or "function",
        "function": {
            "name": getattr(function, "name", "") or "",
            "arguments": getattr(function, "arguments", "") or "",
        },
    }


async def complete(
    llm_client: Any,
    api_kwargs: Dict[str, Any],
    *,
    stream: bool = False,
    on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
) -> CompletedMessage:
    """Run one chat completion and return it fully assembled.

    `on_delta` is awaited with each safe fragment of content as it arrives, and only when
    `stream` is true. It is never called with tool-call fragments: a half-parsed tool call
    is not something to show anyone, and the loop announces tools through its own
    `tool_start` frame once the arguments are complete and parsed.
    """
    if not stream:
        response = await llm_client.chat.completions.create(**api_kwargs)
        message = response.choices[0].message
        return CompletedMessage(
            content=message.content or "",
            tool_calls=[_call_to_dict(c) for c in (message.tool_calls or [])],
            reasoning=getattr(message, "reasoning_content", None),
        )

    emitter = _SafeEmitter(on_delta)
    reasoning_parts: List[str] = []
    # Keyed by the delta's `index`, which is the only thing tying fragments of the same
    # call together — ids and names arrive once, arguments arrive in pieces, and the order
    # across several concurrent calls is not guaranteed.
    calls: Dict[int, Dict[str, Any]] = {}

    response = await llm_client.chat.completions.create(**api_kwargs, stream=True)
    async for chunk in response:
        choices = getattr(chunk, "choices", None)
        if not choices:
            # Usage-only chunks carry no choices at all; skipping them is not an error.
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue

        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            reasoning_parts.append(reasoning)

        await emitter.feed(getattr(delta, "content", None) or "")

        for fragment in getattr(delta, "tool_calls", None) or []:
            index = getattr(fragment, "index", 0) or 0
            entry = calls.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if getattr(fragment, "id", None):
                entry["id"] = fragment.id
            if getattr(fragment, "type", None):
                entry["type"] = fragment.type
            function = getattr(fragment, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    # Assigned, not concatenated: providers repeat the name on later
                    # fragments of the same call, and appending would produce
                    # "get_rowget_row" — a tool that does not exist, failing at dispatch
                    # with an error naming a function nobody wrote.
                    entry["function"]["name"] = function.name
                if getattr(function, "arguments", None):
                    entry["function"]["arguments"] += function.arguments

    await emitter.flush()

    return CompletedMessage(
        content=emitter.full,
        # Ordered by index so the loop dispatches them in the order the model asked for.
        tool_calls=[calls[i] for i in sorted(calls)],
        reasoning="".join(reasoning_parts) or None,
        streamed=True,
    )
