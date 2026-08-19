from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class SheetSyncState(Base):
    """Whether one tab's cached rows in `sheet_records` can still be trusted.

    The Redis cache this sits behind answers freshness with a timer: 60 seconds after a scan,
    the data is assumed stale whether or not anything changed, and a tab nobody has touched in
    a month is re-scanned every minute that someone is looking at it. This table replaces the
    timer with a fact — `is_dirty`, set by an actual signal (a Drive push notification, our own
    worker completing a write, or a `modifiedTime` that no longer matches).

    `headers` are stored **stripped**, exactly as `meta.get_header_row` returns them. That is
    not an oversight to correct: `get_tab_matrix`'s contract is stripped headers that callers
    index as `{h.lower().strip(): i}`, and §7.2's "never strip a header" rule governs
    `resolve_column`, which reads schema_config, not this tuple. Preserving raw whitespace here
    would silently change column resolution for every caller.
    """

    __tablename__ = "sheet_sync_state"

    spreadsheet_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tab: Mapped[str] = mapped_column(String(255), primary_key=True)

    headers: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # The offset the cached rows were scanned at. A re-detected schema can move data_start_row,
    # and every cached row_number is relative to it — so a caller arriving with a different
    # start row is asking about different data, and must be treated as a miss rather than
    # served rows that are off by however much the header block grew.
    data_start_row: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Drive's RFC-3339 modifiedTime as of the last successful sync. Compared as an opaque
    # string; we only ever ask whether it changed, never how much time passed.
    drive_modified_time: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Defaults true: a state row that exists but has never completed a sync must not be
    # mistaken for a clean one.
    is_dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last time freshness was *confirmed* (a matching modifiedTime), which is not the same as
    # the last time data was fetched.
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
