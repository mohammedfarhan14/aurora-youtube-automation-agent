from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class UserCredential(Base):
    __tablename__ = "user_credentials"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ExternalIdentity(Base):
    __tablename__ = "external_identities"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_external_identities_provider_subject",
        ),
        UniqueConstraint(
            "user_id",
            "provider",
            name="uq_external_identities_user_provider",
        ),
        CheckConstraint(
            "provider IN ('google')",
            name="valid_provider",
        ),
        CheckConstraint(
            "length(trim(provider_subject)) > 0",
            name="provider_subject_not_empty",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="google",
        server_default="google",
    )

    provider_subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    provider_email: Mapped[str | None] = mapped_column(
        String(320),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
