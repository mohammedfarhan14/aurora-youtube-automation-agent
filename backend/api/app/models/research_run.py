from datetime import date, datetime
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
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "length(trim(topic)) > 0",
            name="topic_not_empty",
        ),
        CheckConstraint(
            "status IN ('queued', 'researching', 'completed', 'failed')",
            name="valid_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_non_negative",
        ),
        UniqueConstraint("id", "organization_id", name="uq_research_runs_id_organization"),
        UniqueConstraint(
            "organization_id",
            "youtube_channel_id",
            "scheduled_local_date",
            name="uq_research_runs_org_channel_local_day",
        ),
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
            name="fk_research_runs_youtube_channel_tenant",
        ),
        ForeignKeyConstraint(
            ["channel_profile_id", "organization_id"],
            ["youtube_channel_profiles.id", "youtube_channel_profiles.organization_id"],
            ondelete="CASCADE",
            name="fk_research_runs_channel_profile_tenant",
        ),
        Index("ix_research_runs_organization_created", "organization_id", "created_at"),
        Index("ix_research_runs_status_created", "status", "created_at"),
        Index(
            "ix_research_runs_queue_available",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_research_runs_lease_expiry",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    youtube_channel_id: Mapped[UUID | None] = mapped_column(nullable=True)
    channel_profile_id: Mapped[UUID | None] = mapped_column(nullable=True)
    channel_profile_version: Mapped[int | None] = mapped_column(nullable=True)
    channel_profile_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    scheduled_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="queued",
        server_default="queued",
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    lease_token: Mapped[UUID | None] = mapped_column(
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

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
