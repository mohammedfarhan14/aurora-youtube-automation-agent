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


class ResearchSource(Base):
    __tablename__ = "research_sources"
    __table_args__ = (
        CheckConstraint(
            "length(trim(canonical_url)) > 0",
            name="canonical_url_not_empty",
        ),
        CheckConstraint(
            "source_type IN ('webpage', 'paper', 'video', 'news', 'other')",
            name="valid_source_type",
        ),
        CheckConstraint(
            "source_class IN ('scholarly', 'journalism', 'video', 'reference', "
            "'community', 'official_domain', 'unknown')",
            name="valid_source_class",
        ),
        CheckConstraint(
            "independence_basis IN ('unassessed', 'unique_source', 'publisher_domain', "
            "'exact_content', 'near_duplicate_content')",
            name="valid_independence_basis",
        ),
        Index(
            "ix_research_sources_run_independence",
            "research_run_id",
            "independence_group_key",
        ),
        UniqueConstraint(
            "research_run_id",
            "canonical_url",
            name="uq_research_sources_run_canonical_url",
        ),
        Index("ix_research_sources_run_created", "research_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    research_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(250), nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="webpage",
        server_default="webpage",
    )

    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # SHA-256 of the normalized retrieved source content, when available.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publisher_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_class: Mapped[str] = mapped_column(
        String(40), nullable=False, default="unknown", server_default="unknown"
    )
    independence_group_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    independence_basis: Mapped[str] = mapped_column(
        String(40), nullable=False, default="unassessed", server_default="unassessed"
    )
    classification_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="0", server_default="0"
    )
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
