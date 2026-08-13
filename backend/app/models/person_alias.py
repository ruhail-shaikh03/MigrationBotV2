from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.engine import Base


class PersonAlias(Base):
    """One spelling of a person's name, and who it actually is.

    Per-project, because two organisations share no staff. Deliberately dumb: one row per
    (alias, canonical) pair, so a single alias may name several people — which is how a
    comma-separated shared cell is expressed without teaching the splitter about commas.

    Aliases are only ever applied, never inferred. Merging two names hides one inside the
    other and the error is silent — nobody notices a colleague who stopped existing —
    unlike a wrong split or a wrong role, which are visible immediately. On the reference
    tracker "Abdullah" plausibly matches three different people, and nothing in the sheet
    can say which; any rule confident enough to resolve that is confident enough to be
    wrong.
    """

    __tablename__ = "person_aliases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stored already normalised (people.py:normalise_person) so lookup is a plain dict hit.
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    # Display form, spelled as the reader should see it.
    canonical: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "alias", "canonical", name="uq_project_alias_canonical"),
    )

    def __repr__(self) -> str:
        return f"<PersonAlias project={self.project_id} {self.alias!r} -> {self.canonical!r}>"
