from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.production_run import ProductionRun


class ProductionStage(Base):
    """Durable, retryable stage execution and provenance within a production run."""

    __tablename__ = "production_stages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        UniqueConstraint("production_run_id", "stage_key", name="uq_production_stages_run_key"),
        UniqueConstraint("id", "production_run_id", name="uq_production_stages_id_run"),
        Index("ix_production_stages_status_created", "status", "created_at"),
        Index("ix_production_stages_queue_available", "status", "next_attempt_at", "created_at"),
        Index("ix_production_stages_lease_expiry", "status", "lease_expires_at"),
    )

    production_run: Mapped["ProductionRun"] = relationship(back_populates="stages")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    production_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("production_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    input_manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    output_manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
