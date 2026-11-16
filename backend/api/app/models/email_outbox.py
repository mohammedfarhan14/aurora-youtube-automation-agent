from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed')",
            name="valid_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="non_negative_attempts",
        ),
        CheckConstraint(
            "key_version > 0",
            name="positive_key_version",
        ),
        CheckConstraint(
            """
            (
                status = 'processing'
                AND locked_at IS NOT NULL
                AND locked_by IS NOT NULL
            )
            OR
            (
                status != 'processing'
                AND locked_at IS NULL
                AND locked_by IS NULL
            )
            """,
            name="processing_lease_consistent",
        ),
        CheckConstraint(
            """
            (status = 'sent' AND sent_at IS NOT NULL)
            OR
            (status != 'sent' AND sent_at IS NULL)
            """,
            name="sent_timestamp_consistent",
        ),
        Index(
            "ix_email_outbox_pending_available",
            "available_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_email_outbox_processing_locked",
            "locked_at",
            postgresql_where=text("status = 'processing'"),
        ),
        Index(
            "ix_email_outbox_user_id",
            "user_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )

    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    recipient: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
    )

    subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    message_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    encrypted_payload: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    key_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )

    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    locked_by: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    last_error: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )