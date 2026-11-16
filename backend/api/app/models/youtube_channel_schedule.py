from datetime import date, datetime, time
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class YouTubeChannelSchedule(Base):
    """Durable per-channel daily schedule and catch-up policy."""

    __tablename__ = "youtube_channel_schedules"
    __table_args__ = (
        CheckConstraint(
            "catch_up_policy IN ('skip', 'run_once')",
            name="valid_catch_up_policy",
        ),
        CheckConstraint(
            "(enabled = false AND next_run_at IS NULL) OR "
            "(enabled = true AND next_run_at IS NOT NULL)",
            name="enabled_next_run_pair",
        ),
        UniqueConstraint(
            "youtube_channel_id",
            name="uq_youtube_channel_schedules_channel",
        ),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_channel_schedules_channel_tenant",
        ),
        Index("ix_youtube_channel_schedules_due", "enabled", "next_run_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    youtube_channel_id: Mapped[UUID] = mapped_column(nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    posting_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    catch_up_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="skip", server_default="skip"
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scheduled_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
