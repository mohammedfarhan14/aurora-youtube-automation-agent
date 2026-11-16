"""Persist tenant-scoped daily YouTube Analytics snapshots.

Revision ID: 31a8c5e2f704
Revises: f2c8a1d9e630
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "31a8c5e2f704"
down_revision: str | Sequence[str] | None = "f2c8a1d9e630"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "youtube_analytics_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_channel_id", sa.Uuid(), nullable=False),
        sa.Column("report_day", sa.Date(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("minutes_watched", sa.Float(), nullable=False),
        sa.Column("average_view_duration_seconds", sa.Float(), nullable=False),
        sa.Column("average_view_percentage", sa.Float(), nullable=False),
        sa.Column("subscribers_gained", sa.Integer(), nullable=False),
        sa.Column("subscribers_lost", sa.Integer(), nullable=False),
        sa.Column("likes", sa.Integer(), nullable=False),
        sa.Column("comments", sa.Integer(), nullable=False),
        sa.Column("shares", sa.Integer(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_analytics_snapshots_channel_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "youtube_channel_id",
            "report_day",
            name="uq_youtube_analytics_snapshots_channel_day",
        ),
        sa.CheckConstraint("views >= 0", name="ck_youtube_analytics_snapshots_views_nonnegative"),
        sa.CheckConstraint(
            "minutes_watched >= 0 AND average_view_duration_seconds >= 0 "
            "AND average_view_percentage >= 0",
            name="ck_youtube_analytics_snapshots_durations_nonnegative",
        ),
        sa.CheckConstraint(
            "subscribers_gained >= 0 AND subscribers_lost >= 0 "
            "AND likes >= 0 AND comments >= 0 AND shares >= 0",
            name="ck_youtube_analytics_snapshots_counts_nonnegative",
        ),
    )


def downgrade() -> None:
    op.drop_table("youtube_analytics_snapshots")
