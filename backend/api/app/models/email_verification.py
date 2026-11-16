from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    __table_args__ = (
        CheckConstraint(
            "length(token_hash) = 64",
            name="token_hash_length",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="valid_expiration",
        ),
        Index(
            "uq_email_verification_tokens_user_pending",
            "user_id",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
        Index(
            "ix_email_verification_tokens_expiration",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
