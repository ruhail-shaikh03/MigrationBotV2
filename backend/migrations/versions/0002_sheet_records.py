"""The durable row cache: sheet_records, sheet_sync_state, sheet_watch_channels.

Revision ID: 0002_sheet_records
Revises: 0001_baseline
Create Date: 2026-08-19

Three tables holding derived data only. Google Sheets remains the system of record; every
row here is reproducible by re-scanning it. That is what makes `downgrade()` safe to run
without ceremony - dropping these costs one scan per tab, not a single fact.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_sheet_records"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sheet_records",
        sa.Column("spreadsheet_id", sa.String(length=255), nullable=False),
        sa.Column("tab", sa.String(length=255), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("primary_id", sa.String(length=255), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("spreadsheet_id", "tab", "row_number"),
    )
    # Non-unique on purpose: 27 of 412 rows on the reference tracker share a WRICEF ID
    # (TDD §16.7), and a unique index would silently discard the second occurrence of each
    # on every refill.
    op.create_index(
        "ix_sheet_records_primary_id",
        "sheet_records",
        ["spreadsheet_id", "tab", "primary_id"],
        unique=False,
    )

    op.create_table(
        "sheet_sync_state",
        sa.Column("spreadsheet_id", sa.String(length=255), nullable=False),
        sa.Column("tab", sa.String(length=255), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("data_start_row", sa.Integer(), server_default="0", nullable=False),
        sa.Column("drive_modified_time", sa.String(length=64), nullable=True),
        sa.Column("is_dirty", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("spreadsheet_id", "tab"),
    )

    op.create_table(
        "sheet_watch_channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spreadsheet_id", sa.String(length=255), nullable=False),
        sa.Column("channel_id", sa.String(length=255), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("expiration", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_by_email", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sheet_watch_channels_spreadsheet_id",
        "sheet_watch_channels",
        ["spreadsheet_id"],
        unique=True,
    )
    op.create_index(
        "ix_sheet_watch_channels_channel_id",
        "sheet_watch_channels",
        ["channel_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sheet_watch_channels_channel_id", table_name="sheet_watch_channels")
    op.drop_index("ix_sheet_watch_channels_spreadsheet_id", table_name="sheet_watch_channels")
    op.drop_table("sheet_watch_channels")
    op.drop_table("sheet_sync_state")
    op.drop_index("ix_sheet_records_primary_id", table_name="sheet_records")
    op.drop_table("sheet_records")
