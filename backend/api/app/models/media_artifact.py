from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class MediaArtifact(Base):
    """Tenant-bound metadata for an immutable media object stored outside PostgreSQL."""

    __tablename__ = "media_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('audio', 'image', 'video', 'captions', 'thumbnail')",
            name="valid_artifact_kind",
        ),
        CheckConstraint(
            "storage_backend IN ('local', 's3')",
            name="valid_storage_backend",
        ),
        CheckConstraint("length(trim(storage_key)) > 0", name="storage_key_not_empty"),
        CheckConstraint("length(trim(mime_type)) > 0", name="mime_type_not_empty"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint(
            "sha256 ~ '^[a-f0-9]{64}$'",
            name="valid_sha256",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="duration_non_negative",
        ),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(width IS NOT NULL AND height IS NOT NULL AND width > 0 AND height > 0)",
            name="dimensions_pair_positive",
        ),
        UniqueConstraint(
            "organization_id",
            "storage_backend",
            "storage_key",
            name="uq_media_artifacts_org_storage_key",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_media_artifacts_id_org",
        ),
        ForeignKeyConstraint(
            ["production_run_id", "organization_id"],
            ["production_runs.id", "production_runs.organization_id"],
            ondelete="CASCADE",
            name="fk_media_artifacts_run_tenant",
        ),
        ForeignKeyConstraint(
            ["production_stage_id", "production_run_id"],
            ["production_stages.id", "production_stages.production_run_id"],
            ondelete="CASCADE",
            name="fk_media_artifacts_stage_run",
        ),
        Index(
            "ix_media_artifacts_run_created",
            "organization_id",
            "production_run_id",
            "created_at",
        ),
        Index(
            "ix_media_artifacts_stage",
            "production_stage_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    production_run_id: Mapped[UUID] = mapped_column(nullable=False)
    production_stage_id: Mapped[UUID] = mapped_column(nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
