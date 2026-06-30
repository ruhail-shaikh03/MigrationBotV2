from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Boolean, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import List, TYPE_CHECKING
from app.db.engine import Base
from sqlalchemy import text

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.permission import Permission
    from app.models.session import Session

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    spreadsheet_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    default_tab: Mapped[str | None] = mapped_column(String(100), nullable=True)
    company_prefix: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    schema_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    creator: Mapped["User"] = relationship("User", back_populates="created_projects")
    permissions: Mapped[List["Permission"]] = relationship("Permission", back_populates="project", cascade="all, delete-orphan")
    sessions: Mapped[List["Session"]] = relationship("Session", back_populates="project", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Project id={self.id} name={self.project_name} spreadsheet_id={self.spreadsheet_id}>"
