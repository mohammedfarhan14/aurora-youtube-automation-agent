from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class StoryArtifactRecord(Base):
    """Append-only, versioned story artifact with tenant and stage provenance."""

    __tablename__ = "story_artifacts"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_sha256"),
        CheckConstraint(
            "artifact_type IN ('story_draft', 'story_verified')",
            name="valid_artifact_type",
        ),
        UniqueConstraint(
            "production_run_id",
            "artifact_type",
            "version",
            name="uq_story_artifacts_run_type_version",
        ),
        Index(
            "ix_story_artifacts_org_run_created",
            "organization_id",
            "production_run_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    production_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("production_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    production_stage_id: Mapped[UUID] = mapped_column(
        ForeignKey("production_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
