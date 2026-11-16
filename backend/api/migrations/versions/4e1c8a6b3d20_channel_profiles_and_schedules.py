"""bind production runs to versioned channel profiles and schedules

Revision ID: 4e1c8a6b3d20
Revises: 3d8a6f1c9b20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e1c8a6b3d20"
down_revision: str | Sequence[str] | None = "3d8a6f1c9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_youtube_channels_id_org",
        "youtube_channels",
        ["id", "organization_id"],
    )
    op.create_table(
        "youtube_channel_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_channel_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_youtube_channel_profiles_positive_version"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            name="fk_youtube_channel_profiles_channel_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "youtube_channel_id",
            "version",
            name="uq_youtube_channel_profiles_channel_version",
        ),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_youtube_channel_profiles_id_org",
        ),
    )
    op.create_table(
        "youtube_channel_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_channel_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("posting_time", sa.Time(timezone=False), nullable=False),
        sa.Column("catch_up_policy", sa.String(length=20), server_default="skip", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scheduled_local_date", sa.Date(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
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
            "catch_up_policy IN ('skip', 'run_once')",
            name="ck_youtube_channel_schedules_valid_catch_up_policy",
        ),
        sa.CheckConstraint(
            "(enabled = false AND next_run_at IS NULL) OR "
            "(enabled = true AND next_run_at IS NOT NULL)",
            name="ck_youtube_channel_schedules_enabled_next_run_pair",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            name="fk_youtube_channel_schedules_channel_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "youtube_channel_id",
            name="uq_youtube_channel_schedules_channel",
        ),
    )
    op.create_index(
        "ix_youtube_channel_schedules_due",
        "youtube_channel_schedules",
        ["enabled", "next_run_at"],
    )

    op.add_column("production_runs", sa.Column("youtube_channel_id", sa.Uuid(), nullable=True))
    op.add_column("production_runs", sa.Column("channel_profile_id", sa.Uuid(), nullable=True))
    op.add_column(
        "production_runs",
        sa.Column("channel_profile_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "production_runs",
        sa.Column("channel_profile_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "production_runs",
        sa.Column("scheduled_local_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "production_runs",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_production_runs_youtube_channel_tenant",
        "production_runs",
        "youtube_channels",
        ["youtube_channel_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_production_runs_channel_profile_tenant",
        "production_runs",
        "youtube_channel_profiles",
        ["channel_profile_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_production_runs_channel_profile_snapshot_pair",
        "production_runs",
        "(youtube_channel_id IS NULL AND channel_profile_id IS NULL "
        "AND channel_profile_version IS NULL AND channel_profile_snapshot IS NULL) OR "
        "(youtube_channel_id IS NOT NULL AND channel_profile_id IS NOT NULL "
        "AND channel_profile_version IS NOT NULL AND channel_profile_snapshot IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_production_runs_scheduled_date_timestamp_pair",
        "production_runs",
        "(scheduled_local_date IS NULL AND scheduled_for IS NULL) OR "
        "(scheduled_local_date IS NOT NULL AND scheduled_for IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_production_runs_org_channel_local_day",
        "production_runs",
        ["organization_id", "youtube_channel_id", "scheduled_local_date"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_production_runs_org_channel_local_day",
        "production_runs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_production_runs_scheduled_date_timestamp_pair",
        "production_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_production_runs_channel_profile_snapshot_pair",
        "production_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_production_runs_channel_profile_tenant",
        "production_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_production_runs_youtube_channel_tenant",
        "production_runs",
        type_="foreignkey",
    )
    op.drop_column("production_runs", "scheduled_for")
    op.drop_column("production_runs", "scheduled_local_date")
    op.drop_column("production_runs", "channel_profile_snapshot")
    op.drop_column("production_runs", "channel_profile_version")
    op.drop_column("production_runs", "channel_profile_id")
    op.drop_column("production_runs", "youtube_channel_id")
    op.drop_index("ix_youtube_channel_schedules_due", table_name="youtube_channel_schedules")
    op.drop_table("youtube_channel_schedules")
    op.drop_table("youtube_channel_profiles")
    op.drop_constraint("uq_youtube_channels_id_org", "youtube_channels", type_="unique")
