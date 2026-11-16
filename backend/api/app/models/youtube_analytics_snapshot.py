from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class YouTubeAnalyticsSnapshot(Base):
    """Latest verified daily metrics captured for one tenant-owned channel."""

    __tablename__ = "youtube_analytics_snapshots"
    __table_args__ = (
        CheckConstraint("views >= 0", name="views_nonnegative"),
        CheckConstraint(
            "minutes_watched >= 0 AND average_view_duration_seconds >= 0 "
            "AND average_view_percentage >= 0",
            name="durations_nonnegative",
        ),
        CheckConstraint(
            "subscribers_gained >= 0 AND subscribers_lost >= 0 "
            "AND likes >= 0 AND comments >= 0 AND shares >= 0",
            name="counts_nonnegative",
        ),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_analytics_snapshots_channel_tenant",
        ),
        UniqueConstraint(
            "organization_id",
            "youtube_channel_id",
            "report_day",
            name="uq_youtube_analytics_snapshots_channel_day",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    youtube_channel_id: Mapped[UUID] = mapped_column(nullable=False)
    report_day: Mapped[date] = mapped_column(Date, nullable=False)
    views: Mapped[int] = mapped_column(Integer, nullable=False)
    minutes_watched: Mapped[float] = mapped_column(Float, nullable=False)
    average_view_duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    average_view_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    subscribers_gained: Mapped[int] = mapped_column(Integer, nullable=False)
    subscribers_lost: Mapped[int] = mapped_column(Integer, nullable=False)
    likes: Mapped[int] = mapped_column(Integer, nullable=False)
    comments: Mapped[int] = mapped_column(Integer, nullable=False)
    shares: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
