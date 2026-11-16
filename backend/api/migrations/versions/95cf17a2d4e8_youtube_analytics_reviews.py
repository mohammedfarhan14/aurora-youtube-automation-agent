"""Persist operator decisions on analytics review cues.

Revision ID: 95cf17a2d4e8
Revises: 31a8c5e2f704
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "95cf17a2d4e8"
down_revision: str | Sequence[str] | None = "31a8c5e2f704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "youtube_analytics_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_channel_id", sa.Uuid(), nullable=False),
        sa.Column("current_start_date", sa.Date(), nullable=False),
        sa.Column("current_end_date", sa.Date(), nullable=False),
        sa.Column("previous_start_date", sa.Date(), nullable=False),
        sa.Column("previous_end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("evidence_metrics", sa.JSON(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'dismissed')",
            name="ck_youtube_analytics_reviews_valid_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL) "
            "OR (status IN ('approved', 'dismissed') AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_youtube_analytics_reviews_review_decision_pair",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_analytics_reviews_channel_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "youtube_channel_id",
            "current_start_date",
            "current_end_date",
            name="uq_youtube_analytics_reviews_channel_window",
        ),
    )


def downgrade() -> None:
    op.drop_table("youtube_analytics_reviews")
