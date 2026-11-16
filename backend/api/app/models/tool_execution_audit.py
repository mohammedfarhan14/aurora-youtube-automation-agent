from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ToolExecutionAuditRecord(Base):
    """Immutable, tenant-scoped metadata for one permissioned tool invocation."""

    __tablename__ = "tool_execution_audit"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'failure')",
            name="valid_outcome",
        ),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        CheckConstraint(
            "input_sha256 ~ '^[a-f0-9]{64}$'",
            name="input_digest_sha256",
        ),
        CheckConstraint(
            "output_sha256 IS NULL OR output_sha256 ~ '^[a-f0-9]{64}$'",
            name="output_digest_sha256",
        ),
        Index("ix_tool_execution_audit_org_created", "organization_id", "created_at"),
        Index("ix_tool_execution_audit_request", "organization_id", "request_id"),
        Index("ix_tool_execution_audit_tool", "organization_id", "tool_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(120), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
