from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import TYPE_CHECKING
from app.db.engine import Base
from sqlalchemy import text

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.project import Project

class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="editor", server_default="editor")
   # The fixed versions:
    allowed_fields = mapped_column(JSONB, server_default=text("'[\"*\"]'::jsonb"), nullable=False)
    denied_operations = mapped_column(JSONB, server_default=text("'[]'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="permissions")
    project: Mapped["Project"] = relationship("Project", back_populates="permissions")

    # Constraints
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_user_project"),
        CheckConstraint("role IN ('admin', 'editor', 'viewer')", name="chk_role_values"),
    )

    def __repr__(self) -> str:
        return f"<Permission id={self.id} user_id={self.user_id} project_id={self.project_id} role={self.role}>"
