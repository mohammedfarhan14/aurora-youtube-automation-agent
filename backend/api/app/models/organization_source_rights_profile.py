from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class OrganizationSourceRightsProfile(Base):
    """Tenant-scoped reviewed rights policy reusable across research runs."""

    __tablename__ = "organization_source_rights_profiles"
    __table_args__ = (
        CheckConstraint(
            "canonical_url_hash ~ '^[a-f0-9]{64}$'",
            name="canonical_url_hash_sha256",
        ),
        UniqueConstraint(
            "organization_id",
            "canonical_url_hash",
            name="uq_org_source_rights_profile_org_url_hash",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_org_source_rights_profiles_id_org",
        ),
        Index(
            "ix_org_source_rights_profiles_org_updated",
            "organization_id",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="CASCADE",
            name="fk_org_source_rights_profiles_organization",
        ),
        nullable=False,
    )
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    rights_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
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
