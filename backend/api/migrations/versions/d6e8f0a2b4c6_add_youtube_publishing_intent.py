"""add durable YouTube publishing intent and consent

Revision ID: d6e8f0a2b4c6
Revises: c4d6e8f0a2b4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e8f0a2b4c6"
down_revision: str | Sequence[str] | None = "c4d6e8f0a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "youtube_channels",
        sa.Column(
            "auto_publish_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "youtube_channels",
        sa.Column("auto_publish_consent_granted_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "youtube_channels",
        sa.Column(
            "auto_publish_consent_granted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_youtube_channels_auto_publish_consent_actor",
        "youtube_channels",
        "users",
        ["auto_publish_consent_granted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_youtube_channels_auto_publish_consent_pair",
        "youtube_channels",
        "auto_publish_enabled = false OR "
        "(auto_publish_consent_granted_by_user_id IS NOT NULL "
        "AND auto_publish_consent_granted_at IS NOT NULL)",
    )

    op.create_unique_constraint(
        "uq_media_artifacts_id_org",
        "media_artifacts",
        ["id", "organization_id"],
    )

    op.create_table(
        "youtube_publishing_consent_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_channel_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("granted_scopes_snapshot", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('enabled', 'disabled')",
            name="valid_action",
        ),
        sa.ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            name="fk_youtube_publishing_consent_channel_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_youtube_publishing_consent_actor",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_youtube_publishing_consent_channel_created",
        "youtube_publishing_consent_events",
        ["youtube_channel_id", "created_at"],
    )

    op.create_table(
        "youtube_publish_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("production_run_id", sa.Uuid(), nullable=False),
        sa.Column("youtube_channel_id", sa.Uuid(), nullable=False),
        sa.Column("media_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "privacy_status",
            sa.String(length=16),
            server_default="private",
            nullable=False,
        ),
        sa.Column("publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "tags",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "metadata_snapshot",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "bytes_uploaded",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("upload_session_url_encrypted", sa.Text(), nullable=True),
        sa.Column("upload_session_key_version", sa.Integer(), nullable=True),
        sa.Column(
            "upload_session_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("youtube_video_id", sa.String(length=128), nullable=True),
        sa.Column("youtube_processing_status", sa.String(length=40), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'initiating', 'uploading', 'processing', "
            "'published', 'failed', 'cancelled', 'reconciliation_required')",
            name="valid_status",
        ),
        sa.CheckConstraint(
            "privacy_status IN ('private', 'unlisted', 'public')",
            name="valid_privacy_status",
        ),
        sa.CheckConstraint(
            "publish_at IS NULL OR privacy_status = 'private'",
            name="scheduled_publication_private_only",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="idempotency_key_not_empty",
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0",
            name="title_not_empty",
        ),
        sa.CheckConstraint("length(title) <= 100", name="title_max_length"),
        sa.CheckConstraint(
            "length(description) <= 5000",
            name="description_max_length",
        ),
        sa.CheckConstraint("byte_size > 0", name="byte_size_positive"),
        sa.CheckConstraint(
            "bytes_uploaded >= 0 AND bytes_uploaded <= byte_size",
            name="bytes_uploaded_within_total",
        ),
        sa.CheckConstraint(
            "storage_backend IN ('local', 's3')",
            name="valid_storage_backend",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$'",
            name="valid_sha256",
        ),
        sa.CheckConstraint(
            "(upload_session_url_encrypted IS NULL AND upload_session_key_version IS NULL) OR "
            "(upload_session_url_encrypted IS NOT NULL AND upload_session_key_version IS NOT NULL)",
            name="upload_session_encryption_pair",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_token_expiry_pair",
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id", "organization_id"],
            ["production_runs.id", "production_runs.organization_id"],
            name="fk_youtube_publish_jobs_run_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            name="fk_youtube_publish_jobs_channel_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["media_artifact_id", "organization_id"],
            ["media_artifacts.id", "media_artifacts.organization_id"],
            name="fk_youtube_publish_jobs_artifact_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_youtube_publish_jobs_requester",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_youtube_publish_jobs_approver",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_youtube_publish_jobs_org_idempotency",
        ),
        sa.UniqueConstraint(
            "production_run_id",
            name="uq_youtube_publish_jobs_production_run",
        ),
        sa.UniqueConstraint(
            "youtube_channel_id",
            "youtube_video_id",
            name="uq_youtube_publish_jobs_channel_video",
        ),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_youtube_publish_jobs_id_org",
        ),
    )
    op.create_index(
        "ix_youtube_publish_jobs_queue",
        "youtube_publish_jobs",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_youtube_publish_jobs_org_created",
        "youtube_publish_jobs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_youtube_publish_jobs_lease_expiry",
        "youtube_publish_jobs",
        ["status", "lease_expires_at"],
    )

    op.create_table(
        "youtube_publish_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("publish_job_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column(
            "event_metadata",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["publish_job_id", "organization_id"],
            ["youtube_publish_jobs.id", "youtube_publish_jobs.organization_id"],
            name="fk_youtube_publish_events_job_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_youtube_publish_events_actor",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_youtube_publish_events_job_created",
        "youtube_publish_events",
        ["publish_job_id", "created_at"],
    )
    op.create_index(
        "ix_youtube_publish_events_org_created",
        "youtube_publish_events",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_youtube_publish_events_org_created",
        table_name="youtube_publish_events",
    )
    op.drop_index(
        "ix_youtube_publish_events_job_created",
        table_name="youtube_publish_events",
    )
    op.drop_table("youtube_publish_events")
    op.drop_index(
        "ix_youtube_publish_jobs_lease_expiry",
        table_name="youtube_publish_jobs",
    )
    op.drop_index(
        "ix_youtube_publish_jobs_org_created",
        table_name="youtube_publish_jobs",
    )
    op.drop_index(
        "ix_youtube_publish_jobs_queue",
        table_name="youtube_publish_jobs",
    )
    op.drop_table("youtube_publish_jobs")
    op.drop_index(
        "ix_youtube_publishing_consent_channel_created",
        table_name="youtube_publishing_consent_events",
    )
    op.drop_table("youtube_publishing_consent_events")
    op.drop_constraint(
        "uq_media_artifacts_id_org",
        "media_artifacts",
        type_="unique",
    )
    op.drop_constraint(
        "ck_youtube_channels_auto_publish_consent_pair",
        "youtube_channels",
        type_="check",
    )
    op.drop_constraint(
        "fk_youtube_channels_auto_publish_consent_actor",
        "youtube_channels",
        type_="foreignkey",
    )
    op.drop_column("youtube_channels", "auto_publish_consent_granted_at")
    op.drop_column("youtube_channels", "auto_publish_consent_granted_by_user_id")
    op.drop_column("youtube_channels", "auto_publish_enabled")
