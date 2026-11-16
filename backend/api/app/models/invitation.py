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


class OrganizationInvitation(Base):
    __tablename__ = "organization_invitations"

    __table_args__ = (
        CheckConstraint(
            "email = lower(trim(email))",
            name="email_normalized",
        ),
        CheckConstraint(
            "role IN ('admin', 'editor', 'viewer')",
            name="valid_role",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked')",
            name="valid_status",
        ),
        CheckConstraint(
            """
            (status = 'accepted' AND accepted_at IS NOT NULL)
            OR
            (status IN ('pending', 'revoked') AND accepted_at IS NULL)
            """,
            name="accepted_at_consistent",
        ),
        Index(
            "uq_pending_organization_invitations_email",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_organization_invitations_email",
            "email",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )

    invited_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="pending",
        server_default="pending",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
