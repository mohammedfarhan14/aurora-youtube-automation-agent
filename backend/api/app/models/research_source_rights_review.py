from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ResearchSourceRightsReview(Base):
    """Immutable tenant-scoped audit record for a source-rights decision."""

    __tablename__ = "research_source_rights_reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('cleared', 'restricted')",
            name="valid_decision",
        ),
        CheckConstraint(
            "length(trim(review_notes)) > 0",
            name="review_notes_not_empty",
        ),
        ForeignKeyConstraint(
            ["rights_profile_id", "organization_id"],
            [
                "organization_source_rights_profiles.id",
                "organization_source_rights_profiles.organization_id",
            ],
            ondelete="CASCADE",
            name="fk_source_rights_reviews_profile_tenant",
        ),
        Index(
            "ix_source_rights_reviews_source_created",
            "research_source_id",
            "created_at",
        ),
        Index(
            "ix_source_rights_reviews_profile_created",
            "rights_profile_id",
            "created_at",
        ),
        Index(
            "ix_source_rights_reviews_org_created",
            "organization_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(nullable=False)
    rights_profile_id: Mapped[UUID] = mapped_column(nullable=False)
    research_run_id: Mapped[UUID] = mapped_column(nullable=False)
    research_source_id: Mapped[UUID] = mapped_column(nullable=False)
    reviewer_user_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_source_rights_reviews_reviewer",
        ),
        nullable=False,
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_urls: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    previous_rights_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )
    resulting_rights_metadata: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
