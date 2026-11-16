from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class YouTubeOAuthAttempt(Base):
    __tablename__ = "youtube_oauth_attempts"
    __table_args__ = (
        CheckConstraint("length(state_hash) = 64", name="state_hash_sha256"),
        Index("ix_youtube_oauth_attempts_expiry", "expires_at"),
        Index("ix_youtube_oauth_attempts_user_org", "user_id", "organization_id"),
        CheckConstraint(
            "(pending_encrypted_refresh_token IS NULL AND pending_key_version IS NULL) OR "
            "(pending_encrypted_refresh_token IS NOT NULL AND pending_key_version IS NOT NULL)",
            name="pending_token_key_pair",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_scopes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="https://www.googleapis.com/auth/youtube.readonly",
        server_default="https://www.googleapis.com/auth/youtube.readonly",
    )
    target_channel_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("youtube_channels.id", ondelete="CASCADE"),
        nullable=True,
    )
    pending_channels: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    pending_encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_granted_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_selection_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    selection_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class YouTubeChannel(Base):
    __tablename__ = "youtube_channels"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "channel_id", name="uq_youtube_channels_org_channel"
        ),
        UniqueConstraint("id", "organization_id", name="uq_youtube_channels_id_org"),
        CheckConstraint("length(trim(channel_id)) > 0", name="channel_id_not_empty"),
        CheckConstraint("length(trim(title)) > 0", name="title_not_empty"),
        CheckConstraint("key_version > 0", name="positive_key_version"),
        CheckConstraint(
            "status IN ('active', 'disconnected')", name="valid_status"
        ),
        CheckConstraint(
            "auto_publish_enabled = false OR "
            "(auto_publish_consent_granted_by_user_id IS NOT NULL "
            "AND auto_publish_consent_granted_at IS NOT NULL "
            "AND auto_publish_consent_event_id IS NOT NULL)",
            name="auto_publish_consent_pair",
        ),
        CheckConstraint(
            "auto_publish_privacy_status IN ('private', 'unlisted', 'public')",
            name="auto_publish_privacy_status",
        ),
        Index("ix_youtube_channels_organization_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    custom_url: Mapped[str | None] = mapped_column(String(255))
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048))
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_scopes: Mapped[str] = mapped_column(Text, nullable=False)
    connected_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    auto_publish_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    auto_publish_privacy_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", server_default="private"
    )
    auto_publish_consent_granted_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    auto_publish_consent_granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    auto_publish_consent_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
