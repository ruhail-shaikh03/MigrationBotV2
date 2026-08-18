"""One turn of a conversation, kept so the conversation survives the socket.

`chat.py` held the whole conversation in a local `message_history = []`, rebuilt from
nothing on every WebSocket connection. That produced a specific and confusing failure
rather than a clean one: the Zustand store is a module-scope singleton, so after the 3-second
reconnect the *user* still sees the conversation on screen while the *model* has forgotten
it entirely. The next follow-up question — "what about the second one?" — is answered
against an empty history, and nothing on screen explains why.

The rows here are stored in OpenAI wire shape rather than in a display shape. That is
deliberate: the loop's own history is what has to be reconstructed exactly, and a lossy
display record could not do it. `tool_call_id` in particular is not optional bookkeeping —
a `tool` message replayed without the `assistant` message whose `tool_calls` it answers is
rejected by the API outright, so the column exists to make a faithful replay possible at
all (see `core/history.py`, which enforces the pairing when trimming).

A new *table*, not new columns on `sessions`, because `db/engine.py:init_db` runs
`Base.metadata.create_all`, which brings up tables that do not exist and never alters ones
that do. A column added to `sessions` would silently never be created on the deploy.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "user" | "assistant" | "tool". The system prompt is never stored: it is rebuilt per
    # turn from the live project, tab and schema, and a stale copy replayed from Postgres
    # would describe a sheet that has since been re-detected.
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # Nullable because an assistant message that only calls tools has no content at all,
    # and coercing that to "" would replay as an empty assistant turn.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Ordering is by `id`, not `created_at`: a whole turn is written in one commit, so its
    # timestamps tie, and a tie broken arbitrarily would reorder a tool result ahead of the
    # call it answers.
    def __repr__(self) -> str:
        return f"<Message id={self.id} session={self.session_id} role={self.role!r}>"
