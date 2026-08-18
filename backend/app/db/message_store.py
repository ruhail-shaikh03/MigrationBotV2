"""Reading and writing a session's conversation.

Kept out of `core/history.py`, which is pure and unit-testable without a database, and out
of `api/chat.py`, which is already long enough that a fourth concern would make the
WebSocket handler hard to follow in one pass.

Every function here fails soft. Persistence is a convenience layered onto a chat that
worked without it: a database hiccup must degrade the conversation to the old in-memory
behaviour, never take the socket down mid-answer. A dropped history is a smaller loss than
a dropped reply.
"""

import logging
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.history import MAX_REPLAY_MESSAGES, to_storage, to_wire
from app.models.message import Message

logger = logging.getLogger("message_store")


async def load_history(
    db: AsyncSession, session_id: Any, limit: int = MAX_REPLAY_MESSAGES
) -> List[Dict[str, Any]]:
    """The most recent messages for a session, oldest-first, in wire shape.

    The `limit` applies to the *newest* messages, so the query orders by `id` descending
    and reverses in Python. Ordering ascending with a limit would return the oldest N and
    replay the beginning of a long conversation instead of its end.
    """
    try:
        result = await db.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
    except Exception as e:
        logger.warning(f"Could not load history for session {session_id}: {e}")
        return []
    rows.reverse()
    return to_wire(rows)


async def save_turn(db: AsyncSession, session_id: Any, messages: List[Dict[str, Any]]) -> int:
    """Append this turn's new messages. Returns how many rows were written.

    `messages` is the *delta* — `run_agentic_loop` returns the whole accumulated history,
    so the caller passes only what is new. Persisting the full return each turn would
    duplicate the entire conversation on every message, growing quadratically.
    """
    rows = to_storage(messages)
    if not rows:
        return 0
    try:
        db.add_all([Message(session_id=session_id, **row) for row in rows])
        await db.commit()
        return len(rows)
    except Exception as e:
        # Roll back so the caller's session stays usable — `chat.py` commits the session's
        # `last_active` on the same transaction immediately after this.
        logger.warning(f"Could not persist {len(rows)} messages for session {session_id}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return 0


async def clear_history(db: AsyncSession, session_id: Any) -> int:
    """Forget a session's conversation. Returns how many rows were removed."""
    try:
        result = await db.execute(select(Message).where(Message.session_id == session_id))
        rows = list(result.scalars().all())
        for row in rows:
            await db.delete(row)
        await db.commit()
        return len(rows)
    except Exception as e:
        logger.warning(f"Could not clear history for session {session_id}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return 0
