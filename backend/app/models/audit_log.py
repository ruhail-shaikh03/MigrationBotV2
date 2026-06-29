from datetime import datetime, date
from uuid import UUID
from sqlalchemy import String, DateTime, Boolean, Text, Computed, Date, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.engine import Base

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    session_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    spreadsheet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sheet_tab: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ricefw_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    args_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Generated column for partitioning-ready queries
    created_month: Mapped[date] = mapped_column(
        Date,
        Computed("(DATE_TRUNC('month', timestamp AT TIME ZONE 'UTC'))::date", persisted=True)
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} user={self.user_email} tool={self.tool_name} ok={self.result_ok}>"
