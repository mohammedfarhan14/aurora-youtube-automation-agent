from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class YouTubeAnalyticsReview(Base):
    """Operator decision on a measured channel analytics comparison."""

    __tablename__ = "youtube_analytics_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'dismissed')", name="valid_status"),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by_user_id IS NULL AND reviewed_at IS NULL) "
            "OR (status IN ('approved', 'dismissed') AND reviewed_by_user_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="review_decision_pair",
        ),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_analytics_reviews_channel_tenant",
        ),
        UniqueConstraint(
            "organization_id",
            "youtube_channel_id",
            "current_start_date",
            "current_end_date",
            name="uq_youtube_analytics_reviews_channel_window",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    youtube_channel_id: Mapped[UUID] = mapped_column(nullable=False)
    current_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    current_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    previous_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    previous_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
