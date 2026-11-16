from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class YouTubeChannelProfile(Base):
    """Immutable version of a channel's editorial instructions."""

    __tablename__ = "youtube_channel_profiles"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        UniqueConstraint(
            "youtube_channel_id",
            "version",
            name="uq_youtube_channel_profiles_channel_version",
        ),
        UniqueConstraint("id", "organization_id", name="uq_youtube_channel_profiles_id_org"),
        ForeignKeyConstraint(
            ["youtube_channel_id", "organization_id"],
            ["youtube_channels.id", "youtube_channels.organization_id"],
            ondelete="CASCADE",
            name="fk_youtube_channel_profiles_channel_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    youtube_channel_id: Mapped[UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
