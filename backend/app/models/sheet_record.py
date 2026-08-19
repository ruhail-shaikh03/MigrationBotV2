from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class SheetRecord(Base):
    """One cached data row of one tab of one tracker spreadsheet.

    This is the durable read model TDD §11.1 described as unbuilt. Postgres previously held
    no spreadsheet data at all (§2); this table and `sheet_sync_state` are the exception,
    and they are exceptions of a specific kind: **derived data**. Nothing here is authoritative.
    Google Sheets is the system of record, every row here is reproducible by re-scanning it,
    and dropping the whole table costs one scan per tab, not a single fact.

    Keyed by `row_number` rather than by the tracker's own ID column, for two reasons. It
    reproduces sheet order exactly, which is what all seven `get_tab_matrix` callers actually
    want. And `primary_id` cannot be a key: §16.7 records 27 of 412 HEDP rows sharing a WRICEF
    No. with another row, so a unique constraint on it would silently discard the second
    occurrence of each on every refill — turning a known reporting oddity into invisible data
    loss. The index on it is deliberately non-unique.

    `data` holds the raw ragged cell list exactly as `read.py:_fetch_all_rows` returned it —
    not padded to the header width, not keyed by header name. Callers already pad and index
    positionally, so storing the scan verbatim is what keeps a cached read byte-identical to
    a live one.
    """

    __tablename__ = "sheet_records"

    spreadsheet_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tab: Mapped[str] = mapped_column(String(255), primary_key=True)
    # The live Sheets row number (data_start_row + i). Stable only until an insert shifts it,
    # which is precisely why a refill replaces every row for the tab rather than upserting.
    row_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    primary_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[list] = mapped_column(JSONB, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Non-unique on purpose — see the class docstring and §16.7.
        Index("ix_sheet_records_primary_id", "spreadsheet_id", "tab", "primary_id"),
    )
