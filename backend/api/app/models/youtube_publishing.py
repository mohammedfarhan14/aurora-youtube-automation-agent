from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class YouTubePublishingConsentEvent(Base):
    """Append-only record of channel-level automatic-publishing consent changes."""

    __tablename__ = "youtube_publishing_consent_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('enabled', 'disabled')",
            name="valid_action",
        ),
        UniqueConstraint(
            "id",
            "youtube_channel_id",
            "organization_id",
            name="uq_youtube_publishing_consent_events_id_channel_org",
        ),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_publishing_consent_channel_tenant",
        ),
        Index(
            "ix_youtube_publishing_consent_channel_created",
            "youtube_channel_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    youtube_channel_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    granted_scopes_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class YouTubePublishJob(Base):
    """Durable, tenant-bound intent and resumable state for one YouTube upload."""

    __tablename__ = "youtube_publish_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'initiating', 'uploading', 'processing', "
            "'uploaded', 'published', 'failed', 'cancelled', 'reconciliation_required')",
            name="valid_status",
        ),
        CheckConstraint(
            "("
            "approval_mode = 'human' AND approved_by_user_id IS NOT NULL "
            "AND approval_consent_event_id IS NULL"
            ") OR ("
            "approval_mode = 'automatic_consent' AND approved_by_user_id IS NULL "
            "AND approval_consent_event_id IS NOT NULL"
            ")",
            name="approval_mode_consistency",
        ),
        CheckConstraint(
            "privacy_status IN ('private', 'unlisted', 'public')",
            name="valid_privacy_status",
        ),
        CheckConstraint(
            "publish_at IS NULL OR privacy_status = 'private'",
            name="scheduled_publication_private_only",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="idempotency_key_not_empty",
        ),
        CheckConstraint(
            "length(trim(title)) > 0",
            name="title_not_empty",
        ),
        CheckConstraint("length(title) <= 100", name="title_max_length"),
        CheckConstraint(
            "length(description) <= 5000",
            name="description_max_length",
        ),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint(
            "bytes_uploaded >= 0 AND bytes_uploaded <= byte_size",
            name="bytes_uploaded_within_total",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_non_negative",
        ),
        CheckConstraint(
            "storage_backend IN ('local', 's3')",
            name="valid_storage_backend",
        ),
        CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$'",
            name="valid_sha256",
        ),
        CheckConstraint(
            "(upload_session_url_encrypted IS NULL AND upload_session_key_version IS NULL) OR "
            "(upload_session_url_encrypted IS NOT NULL AND upload_session_key_version IS NOT NULL)",
            name="upload_session_encryption_pair",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_token_expiry_pair",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_youtube_publish_jobs_org_idempotency",
        ),
        Index("ix_youtube_publish_jobs_production_run", "production_run_id"),
        UniqueConstraint(
            "youtube_channel_id",
            "youtube_video_id",
            name="uq_youtube_publish_jobs_channel_video",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_youtube_publish_jobs_id_org",
        ),
        ForeignKeyConstraint(
            ["production_run_id", "organization_id"],
            ["production_runs.id", "production_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_publish_jobs_run_tenant",
        ),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="RESTRICT",
            name="fk_youtube_publish_jobs_channel_tenant",
        ),
        ForeignKeyConstraint(
            ["media_artifact_id", "organization_id"],
            ["media_artifacts.id", "media_artifacts.organization_id"],
            ondelete="RESTRICT",
            name="fk_youtube_publish_jobs_artifact_tenant",
        ),
        ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_youtube_publish_jobs_requester",
        ),
        ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_youtube_publish_jobs_approver",
        ),
        ForeignKeyConstraint(
            ["approval_consent_event_id", "youtube_channel_id", "organization_id"],
            [
                "youtube_publishing_consent_events.id",
                "youtube_publishing_consent_events.youtube_channel_id",
                "youtube_publishing_consent_events.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_youtube_publish_jobs_approval_consent_event_tenant",
        ),
        Index(
            "ix_youtube_publish_jobs_queue",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_youtube_publish_jobs_org_created",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_youtube_publish_jobs_lease_expiry",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    production_run_id: Mapped[UUID] = mapped_column(nullable=False)
    youtube_channel_id: Mapped[UUID] = mapped_column(nullable=False)
    media_artifact_id: Mapped[UUID] = mapped_column(nullable=False)
    requested_by_user_id: Mapped[UUID] = mapped_column(nullable=False)
    approved_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approval_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="human", server_default="human"
    )
    approval_consent_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    privacy_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", server_default="private"
    )
    publish_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bytes_uploaded: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    upload_session_url_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_session_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    youtube_video_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    youtube_processing_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @property
    def thumbnail_upload_warning(self) -> str | None:
        """Return an optional thumbnail warning without exposing the full snapshot."""
        thumbnail = (self.metadata_snapshot or {}).get("thumbnail_artifact")
        warning = thumbnail.get("upload_error") if isinstance(thumbnail, dict) else None
        return warning if isinstance(warning, str) and warning.strip() else None


class YouTubePublishEvent(Base):
    """Sanitized append-only state-transition and reconciliation history."""

    __tablename__ = "youtube_publish_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["publish_job_id", "organization_id"],
            ["youtube_publish_jobs.id", "youtube_publish_jobs.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_publish_events_job_tenant",
        ),
        Index(
            "ix_youtube_publish_events_job_created",
            "publish_job_id",
            "created_at",
        ),
        Index(
            "ix_youtube_publish_events_org_created",
            "organization_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    publish_job_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
