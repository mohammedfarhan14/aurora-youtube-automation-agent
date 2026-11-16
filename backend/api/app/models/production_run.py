from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.production_stage import ProductionStage


class ProductionRun(Base):
    """Tenant-scoped durable orchestration record for one video production."""

    __tablename__ = "production_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'planning', 'generating', 'verifying', "
            "'awaiting_review', 'approved', 'rejected', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint(
            "(status = 'rejected' AND rejected_by_user_id IS NOT NULL "
            "AND rejected_at IS NOT NULL) OR "
            "(status <> 'rejected' AND rejected_by_user_id IS NULL AND rejected_at IS NULL)",
            name="rejection_actor_timestamp_pair",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint(
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
            name="approval_mode_consistency",
        ),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="idempotency_key_not_empty"),
        CheckConstraint(
            "(youtube_channel_id IS NULL AND channel_profile_id IS NULL "
            "AND channel_profile_version IS NULL AND channel_profile_snapshot IS NULL) OR "
            "(youtube_channel_id IS NOT NULL AND channel_profile_id IS NOT NULL "
            "AND channel_profile_version IS NOT NULL AND channel_profile_snapshot IS NOT NULL)",
            name="channel_profile_snapshot_pair",
        ),
        CheckConstraint(
            "(scheduled_local_date IS NULL AND scheduled_for IS NULL) OR "
            "(scheduled_local_date IS NOT NULL AND scheduled_for IS NOT NULL)",
            name="scheduled_date_timestamp_pair",
        ),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_production_runs_youtube_channel_tenant",
        ),
        ForeignKeyConstraint(
            ["channel_profile_id", "organization_id"],
            ["youtube_channel_profiles.id", "youtube_channel_profiles.organization_id"],
            ondelete="CASCADE",
            name="fk_production_runs_channel_profile_tenant",
        ),
        ForeignKeyConstraint(
            ["research_run_id", "organization_id"],
            ["research_runs.id", "research_runs.organization_id"],
            ondelete="RESTRICT",
            name="fk_production_runs_research_run_tenant",
        ),
        ForeignKeyConstraint(
            ["approval_consent_event_id", "youtube_channel_id", "organization_id"],
            [
                "youtube_publishing_consent_events.id",
                "youtube_publishing_consent_events.youtube_channel_id",
                "youtube_publishing_consent_events.organization_id",
            ],
            ondelete="RESTRICT",
            name="fk_production_runs_approval_consent_event_tenant",
        ),
        UniqueConstraint("id", "organization_id", name="uq_production_runs_id_org"),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_production_runs_organization_idempotency",
        ),
        UniqueConstraint(
            "organization_id",
            "youtube_channel_id",
            "scheduled_local_date",
            name="uq_production_runs_org_channel_local_day",
        ),
        Index("ix_production_runs_org_created", "organization_id", "created_at"),
        Index("ix_production_runs_status_created", "status", "created_at"),
        Index("ix_production_runs_lease_expiry", "status", "lease_expires_at"),
    )

    stages: Mapped[list["ProductionStage"]] = relationship(
        back_populates="production_run",
        cascade="all, delete-orphan",
        order_by="ProductionStage.created_at, ProductionStage.id",
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    research_run_id: Mapped[UUID] = mapped_column(
        nullable=False,
    )
    youtube_channel_id: Mapped[UUID | None] = mapped_column(nullable=True)
    channel_profile_id: Mapped[UUID | None] = mapped_column(nullable=True)
    channel_profile_version: Mapped[int | None] = mapped_column(nullable=True)
    channel_profile_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    scheduled_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_production_runs_approved_by_user"
        ),
        nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_warning_override: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    rejected_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="none", server_default="none"
    )
    approval_consent_event_id: Mapped[UUID | None] = mapped_column(nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="queued", server_default="queued"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
