"""record consent-based automatic production approval and publishing policy

Revision ID: c2a7e91f4b30
Revises: e7f9a1b3c5d7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2a7e91f4b30"
down_revision: str | Sequence[str] | None = "e7f9a1b3c5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "youtube_channels",
        sa.Column(
            "auto_publish_privacy_status",
            sa.String(length=16),
            server_default="private",
            nullable=False,
        ),
    )
    op.add_column(
        "youtube_channels",
        sa.Column("auto_publish_consent_event_id", sa.Uuid(), nullable=True),
    )
    op.drop_constraint(
        "ck_youtube_channels_auto_publish_consent_pair",
        "youtube_channels",
        type_="check",
    )
    op.create_check_constraint(
        "ck_youtube_channels_auto_publish_privacy_status",
        "youtube_channels",
        "auto_publish_privacy_status IN ('private', 'unlisted', 'public')",
    )

    op.add_column(
        "production_runs",
        sa.Column(
            "approval_mode",
            sa.String(length=24),
            server_default="none",
            nullable=False,
        ),
    )
    op.add_column(
        "production_runs",
        sa.Column("approval_consent_event_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        "UPDATE production_runs SET approval_mode = 'human' "
        "WHERE approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL"
    )
    op.drop_constraint(
        "ck_production_runs_approval_actor_timestamp_pair",
        "production_runs",
        type_="check",
    )
    op.create_unique_constraint(
        "uq_youtube_publishing_consent_events_id_channel_org",
        "youtube_publishing_consent_events",
        ["id", "youtube_channel_id", "organization_id"],
    )
    op.execute(
        "UPDATE youtube_channels AS channel "
        "SET auto_publish_consent_event_id = ("
        "SELECT event.id FROM youtube_publishing_consent_events AS event "
        "WHERE event.youtube_channel_id = channel.id "
        "AND event.organization_id = channel.organization_id "
        "AND event.action = 'enabled' "
        "AND event.actor_user_id = channel.auto_publish_consent_granted_by_user_id "
        "ORDER BY event.created_at DESC, event.id DESC LIMIT 1"
        ") WHERE channel.auto_publish_enabled = true"
    )
    op.execute(
        "UPDATE youtube_channels SET "
        "auto_publish_enabled = false, "
        "auto_publish_consent_granted_by_user_id = NULL, "
        "auto_publish_consent_granted_at = NULL "
        "WHERE auto_publish_enabled = true AND auto_publish_consent_event_id IS NULL"
    )
    op.create_check_constraint(
        "ck_youtube_channels_auto_publish_consent_pair",
        "youtube_channels",
        "auto_publish_enabled = false OR "
        "(auto_publish_consent_granted_by_user_id IS NOT NULL "
        "AND auto_publish_consent_granted_at IS NOT NULL "
        "AND auto_publish_consent_event_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_production_runs_approval_consent_event_tenant",
        "production_runs",
        "youtube_publishing_consent_events",
        ["approval_consent_event_id", "youtube_channel_id", "organization_id"],
        ["id", "youtube_channel_id", "organization_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_production_runs_approval_mode_consistency",
        "production_runs",
        "("
        "approval_mode = 'none' AND approved_by_user_id IS NULL "
        "AND approved_at IS NULL AND approval_consent_event_id IS NULL"
        ") OR ("
        "approval_mode = 'human' AND approved_by_user_id IS NOT NULL "
        "AND approved_at IS NOT NULL AND approval_consent_event_id IS NULL"
        ") OR ("
        "approval_mode = 'automatic_consent' AND approved_by_user_id IS NULL "
        "AND approved_at IS NOT NULL AND approval_consent_event_id IS NOT NULL"
        ")",
    )

    op.add_column(
        "youtube_publish_jobs",
        sa.Column(
            "approval_mode",
            sa.String(length=24),
            server_default="human",
            nullable=False,
        ),
    )
    op.add_column(
        "youtube_publish_jobs",
        sa.Column("approval_consent_event_id", sa.Uuid(), nullable=True),
    )
    op.alter_column(
        "youtube_publish_jobs",
        "approved_by_user_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.execute(
        "UPDATE youtube_publish_jobs SET metadata_snapshot = ("
        "COALESCE(metadata_snapshot::jsonb, '{}'::jsonb) || "
        "jsonb_build_object("
        "'approval_mode', 'human', "
        "'approval_consent_event_id', NULL, "
        "'auto_publish_privacy_status', NULL"
        "))::json WHERE approval_mode = 'human'"
    )
    op.create_foreign_key(
        "fk_youtube_publish_jobs_approval_consent_event_tenant",
        "youtube_publish_jobs",
        "youtube_publishing_consent_events",
        ["approval_consent_event_id", "youtube_channel_id", "organization_id"],
        ["id", "youtube_channel_id", "organization_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_youtube_publish_jobs_approval_mode_consistency",
        "youtube_publish_jobs",
        "("
        "approval_mode = 'human' AND approved_by_user_id IS NOT NULL "
        "AND approval_consent_event_id IS NULL"
        ") OR ("
        "approval_mode = 'automatic_consent' AND approved_by_user_id IS NULL "
        "AND approval_consent_event_id IS NOT NULL"
        ")",
    )


def downgrade() -> None:
    op.execute(
        "DO $ BEGIN "
        "IF EXISTS (SELECT 1 FROM production_runs "
        "WHERE approval_mode = 'automatic_consent') "
        "OR EXISTS (SELECT 1 FROM youtube_publish_jobs "
        "WHERE approval_mode = 'automatic_consent') THEN "
        "RAISE EXCEPTION 'Cannot downgrade while consent-based automatic approvals exist.'; "
        "END IF; END $;"
    )
    op.drop_constraint(
        "ck_youtube_publish_jobs_approval_mode_consistency",
        "youtube_publish_jobs",
        type_="check",
    )
    op.drop_constraint(
        "fk_youtube_publish_jobs_approval_consent_event_tenant",
        "youtube_publish_jobs",
        type_="foreignkey",
    )
    op.alter_column(
        "youtube_publish_jobs",
        "approved_by_user_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.drop_column("youtube_publish_jobs", "approval_consent_event_id")
    op.drop_column("youtube_publish_jobs", "approval_mode")

    op.drop_constraint(
        "ck_production_runs_approval_mode_consistency",
        "production_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_production_runs_approval_consent_event_tenant",
        "production_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_youtube_publishing_consent_events_id_channel_org",
        "youtube_publishing_consent_events",
        type_="unique",
    )
    op.create_check_constraint(
        "ck_production_runs_approval_actor_timestamp_pair",
        "production_runs",
        "(approved_by_user_id IS NULL AND approved_at IS NULL) OR "
        "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
    )
    op.drop_column("production_runs", "approval_consent_event_id")
    op.drop_column("production_runs", "approval_mode")

    op.drop_constraint(
        "ck_youtube_channels_auto_publish_consent_pair",
        "youtube_channels",
        type_="check",
    )
    op.create_check_constraint(
        "ck_youtube_channels_auto_publish_consent_pair",
        "youtube_channels",
        "auto_publish_enabled = false OR "
        "(auto_publish_consent_granted_by_user_id IS NOT NULL "
        "AND auto_publish_consent_granted_at IS NOT NULL)",
    )
    op.drop_column("youtube_channels", "auto_publish_consent_event_id")
    op.drop_constraint(
        "ck_youtube_channels_auto_publish_privacy_status",
        "youtube_channels",
        type_="check",
    )
    op.drop_column("youtube_channels", "auto_publish_privacy_status")
