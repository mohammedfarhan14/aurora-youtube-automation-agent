"""bind scheduled research runs to channel profiles

Revision ID: a6d9c3e1f742
Revises: 4e1c8a6b3d20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6d9c3e1f742"
down_revision: str | Sequence[str] | None = "4e1c8a6b3d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("research_runs", sa.Column("youtube_channel_id", sa.Uuid(), nullable=True))
    op.add_column("research_runs", sa.Column("channel_profile_id", sa.Uuid(), nullable=True))
    op.add_column(
        "research_runs",
        sa.Column("channel_profile_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "research_runs",
        sa.Column("channel_profile_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "research_runs",
        sa.Column("scheduled_local_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "research_runs",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_research_runs_youtube_channel_tenant",
        "research_runs",
        "youtube_channels",
        ["youtube_channel_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_research_runs_channel_profile_tenant",
        "research_runs",
        "youtube_channel_profiles",
        ["channel_profile_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "channel_profile_snapshot_pair",
        "research_runs",
        "(youtube_channel_id IS NULL AND channel_profile_id IS NULL "
        "AND channel_profile_version IS NULL AND channel_profile_snapshot IS NULL) OR "
        "(youtube_channel_id IS NOT NULL AND channel_profile_id IS NOT NULL "
        "AND channel_profile_version IS NOT NULL AND channel_profile_snapshot IS NOT NULL)",
    )
    op.create_check_constraint(
        "scheduled_date_timestamp_pair",
        "research_runs",
        "(scheduled_local_date IS NULL AND scheduled_for IS NULL) OR "
        "(scheduled_local_date IS NOT NULL AND scheduled_for IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_research_runs_org_channel_local_day",
        "research_runs",
        ["organization_id", "youtube_channel_id", "scheduled_local_date"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_research_runs_org_channel_local_day",
        "research_runs",
        type_="unique",
    )
    op.drop_constraint(
        "scheduled_date_timestamp_pair",
        "research_runs",
        type_="check",
    )
    op.drop_constraint(
        "channel_profile_snapshot_pair",
        "research_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_research_runs_channel_profile_tenant",
        "research_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_research_runs_youtube_channel_tenant",
        "research_runs",
        type_="foreignkey",
    )
    op.drop_column("research_runs", "scheduled_for")
    op.drop_column("research_runs", "scheduled_local_date")
    op.drop_column("research_runs", "channel_profile_snapshot")
    op.drop_column("research_runs", "channel_profile_version")
    op.drop_column("research_runs", "channel_profile_id")
    op.drop_column("research_runs", "youtube_channel_id")
