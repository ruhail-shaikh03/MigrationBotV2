from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class SheetWatchChannel(Base):
    """A live Google Drive push-notification channel for one spreadsheet.

    Per *spreadsheet*, not per tab: Drive watches a file, and its notifications carry no
    indication of which cell — or which tab — changed. That granularity limit is why a
    notification marks every tab of the spreadsheet dirty rather than trying to be surgical.

    `token` is the shared secret handed to Drive at registration and echoed back on every
    notification in `X-Goog-Channel-Token`. It is what authenticates the sender to an endpoint
    that, by necessity, carries no user credentials.

    Channels expire. There is no scheduler in this application to renew them on a timer, so
    renewal is opportunistic (see `sheets/drive.py`) and lapsing is survivable: reads fall back
    to polling `modifiedTime` and simply get chattier.
    """

    __tablename__ = "sheet_watch_channels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    spreadsheet_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    # The UUID we generate and hand to Drive; comes back as X-Goog-Channel-ID.
    channel_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Drive's own identifier for the watched resource, required to stop the channel.
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    expiration: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_by_email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
