from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            r"email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'",
            name="check_email_format",
        ),
        CheckConstraint(
            "length(trim(email)) > 0",
            name="email_not_empty",
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0",
            name="display_name_not_empty",
        ),
        CheckConstraint(
            "email = lower(trim(email))",
            name="email_normalized",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        nullable=False,
    )

    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    email_verified_at: Mapped[datetime | None] = mapped_column(
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
